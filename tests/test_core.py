from __future__ import annotations

import unittest

from layout_analyzer.metrics import kendall_tau_b
from layout_analyzer.model import Block, GraphEdge
from layout_analyzer.reading_order import _topological_sort, build_reading_order
from layout_analyzer.verifier import verify


def make_block(
    uid: str,
    block_type: str,
    bbox: tuple[float, float, float, float],
) -> Block:
    return Block(
        uid=uid,
        bbox=bbox,
        block_type=block_type,
        confidence=0.9,
        segmentation_confidence=0.9,
        features={},
        evidence={"rule": "synthetic_test_rule"},
        component_count=1,
    )


class ReadingOrderTests(unittest.TestCase):
    def test_ambiguous_annotation_is_not_silently_assigned(self) -> None:
        blocks = [
            make_block("c_right", "body_column", (1400, 500, 1480, 1400)),
            make_block("c_mid", "body_column", (1250, 500, 1330, 800)),
            make_block("c_left", "body_column", (1100, 500, 1180, 800)),
            make_block("ann_bridge", "inline_annotation", (1190, 900, 1240, 1200)),
        ]
        result = build_reading_order(blocks, 1600)
        self.assertIn("ann_bridge", result.unresolved)
        self.assertNotIn("ann_bridge", result.order)
        conflicts = [conflict for conflict in result.conflicts if conflict.kind == "ambiguous_annotation_anchor"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(len(conflicts[0].candidate_edges), 2)
        self.assertEqual(set(conflicts[0].involved), {"ann_bridge", "c_mid", "c_left"})

    def test_annotation_is_forced_before_next_body_column(self) -> None:
        blocks = [
            make_block("c_right", "body_column", (1400, 500, 1480, 1400)),
            make_block("c_mid", "body_column", (1250, 500, 1330, 900)),
            make_block("ann_mid", "inline_annotation", (1260, 980, 1318, 1280)),
            make_block("c_left", "body_column", (1100, 500, 1180, 1400)),
        ]
        result = build_reading_order(blocks, 1600)
        self.assertEqual(
            result.order,
            ["c_right", "c_mid", "ann_mid", "c_left"],
        )
        self.assertTrue(
            any(edge.rule == "attachments_precede_next_main_column" for edge in result.edges)
        )

    def test_directed_cycle_is_reported(self) -> None:
        blocks = {
            "a": make_block("a", "body_column", (100, 100, 200, 500)),
            "b": make_block("b", "body_column", (300, 100, 400, 500)),
        }
        edges = [
            GraphEdge("a", "b", "test_cycle"),
            GraphEdge("b", "a", "test_cycle"),
        ]
        order, unresolved, cycle_edges = _topological_sort(set(blocks), edges, blocks)
        self.assertEqual(order, [])
        self.assertEqual(unresolved, {"a", "b"})
        self.assertEqual(len(cycle_edges), 2)


class MetricAndVerifierTests(unittest.TestCase):
    def test_kendall_tau(self) -> None:
        self.assertEqual(kendall_tau_b(["a", "b", "c"], ["a", "b", "c"]), 1.0)
        self.assertEqual(kendall_tau_b(["a", "b", "c"], ["c", "b", "a"]), -1.0)

    def test_verifier_finds_wrong_vertical_direction_and_annotation_placement(self) -> None:
        features = {
            "schema": "gsb-layout-features/v1",
            "blocks": [
                {
                    "uid": "c_left",
                    "type": "body_column",
                    "bbox": [1100, 500, 1180, 1400],
                    "x_center": 1140,
                },
                {
                    "uid": "c_right",
                    "type": "body_column",
                    "bbox": [1400, 500, 1480, 1400],
                    "x_center": 1440,
                },
                {
                    "uid": "ann_right",
                    "type": "inline_annotation",
                    "bbox": [1410, 1460, 1468, 1760],
                    "x_center": 1439,
                },
            ],
        }
        report = {
            "schema": "gsb-layout-page-report/v1",
            "page_id": "bad",
            "format": "test",
            "reading_order": [
                {"uid": "c_left"},
                {"uid": "ann_right"},
                {"uid": "c_right"},
            ],
            "conflict_section": {"unresolved_blocks": [], "conflicts": []},
        }
        result = verify(report, features)
        self.assertFalse(result["ok"])
        rules = {violation["rule"] for violation in result["violations"]}
        self.assertIn("vertical_columns_right_to_left", rules)
        self.assertIn("annotation_follows_anchor_column", rules)


if __name__ == "__main__":
    unittest.main()
