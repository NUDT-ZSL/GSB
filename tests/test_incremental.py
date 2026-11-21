from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from layout_order.cache import ContentAddressedCache
from layout_order.pipeline import LayoutRunner, report_content_fingerprint
from layout_order.samples import generate_dataset
from layout_order.snapshot import replace_snapshot_section


class IncrementalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dataset = generate_dataset(self.root / "data", page_count=3)
        self.snapshot = self.dataset["snapshot"]
        self.limits = self.dataset["config"]["limits"]
        self.runner = LayoutRunner(
            ContentAddressedCache(self.root / "cache"),
            limits=self.limits,
        )
        self.baseline = self.runner.run(self.snapshot, mode="baseline")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_page_edit_recomputes_only_page_suffix(self) -> None:
        pages = copy.deepcopy(
            self.snapshot.section("block_annotations").content
        )
        changed_page = sorted(pages)[0]
        body = next(
            block
            for block in pages[changed_page]["blocks"]
            if block["block_type"] == "body"
        )
        body["x1"] += 5
        body["y1"] += 2
        modified = replace_snapshot_section(
            self.snapshot, "block_annotations", pages
        )
        incremental = self.runner.run(modified, mode="incremental")
        expected = {f"{stage}:{changed_page}" for stage in (
            "normalize",
            "edges",
            "costs",
            "solve",
            "metrics",
        )}
        self.assertEqual(
            set(incremental["execution"]["recomputed_nodes"]), expected
        )
        self.assertEqual(
            incremental["execution"]["unaffected_pages"], sorted(pages)[1:]
        )
        full = LayoutRunner(
            ContentAddressedCache(self.root / "full-cache"),
            limits=self.limits,
        ).run(modified, mode="full")
        self.assertEqual(
            report_content_fingerprint(incremental),
            report_content_fingerprint(full),
        )

    def test_weight_change_reuses_geometry_edges(self) -> None:
        weights = copy.deepcopy(
            self.snapshot.section("edge_weights").content
        )
        weights["weights"]["vertical_gap"] = 7.5
        modified = replace_snapshot_section(
            self.snapshot, "edge_weights", weights
        )
        incremental = self.runner.run(modified, mode="incremental")
        execution = incremental["execution"]
        for node in execution["reused_nodes"]:
            self.assertTrue(
                node.startswith("normalize:") or node.startswith("edges:")
            )
        self.assertEqual(execution["geometry_affected_pages"], [])
        self.assertFalse(incremental["summary"]["degraded"])

    def test_type_only_edit_reuses_candidate_edges(self) -> None:
        pages = copy.deepcopy(
            self.snapshot.section("block_annotations").content
        )
        changed_page = sorted(pages)[0]
        body = next(
            block
            for block in pages[changed_page]["blocks"]
            if block["block_type"] == "body"
        )
        body["block_type"] = "section_title"
        modified = replace_snapshot_section(
            self.snapshot, "block_annotations", pages
        )
        incremental = self.runner.run(modified, mode="incremental")
        self.assertIn(
            f"normalize:{changed_page}",
            incremental["execution"]["recomputed_nodes"],
        )
        self.assertIn(
            f"edges:{changed_page}",
            incremental["execution"]["reused_nodes"],
        )
        self.assertIn(
            f"costs:{changed_page}",
            incremental["execution"]["recomputed_nodes"],
        )


if __name__ == "__main__":
    unittest.main()

