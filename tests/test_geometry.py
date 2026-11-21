from __future__ import annotations

import json
import unittest

from layout_order.errors import LayoutError
from layout_order.geometry import (
    build_candidate_edges,
    canonical_edges_bytes,
    canonical_normalized_bytes,
    normalize_page,
    validate_candidate_coverage,
)
from layout_order.samples import generate_page, generate_pages


class GeometryTests(unittest.TestCase):
    def test_normalization_and_edges_are_byte_deterministic(self) -> None:
        page = generate_pages()[0]
        first_norm = canonical_normalized_bytes(page)
        second_norm = canonical_normalized_bytes(page)
        first_edges = canonical_edges_bytes(page)
        second_edges = canonical_edges_bytes(page)
        self.assertEqual(first_norm, second_norm)
        self.assertEqual(first_edges, second_edges)
        parsed = json.loads(first_edges)
        self.assertTrue(all(edge["source_block_id"] for edge in parsed))

    def test_every_block_is_covered(self) -> None:
        page = generate_pages()[1]
        normalized = normalize_page(page)
        edges = build_candidate_edges(normalized)["edges"]
        validate_candidate_coverage(normalized, edges)
        sources = {edge["source_block_id"] for edge in edges}
        targets = {edge["target_block_id"] for edge in edges}
        self.assertEqual(sources, set(range(1, len(page["blocks"]) + 1)))
        self.assertEqual(targets, set(range(1, len(page["blocks"]) + 1)))

    def test_singleton_page_gets_self_loop(self) -> None:
        page = generate_page(7, 0, "single_column")
        page["blocks"] = [page["blocks"][0]]
        page["blocks"][0]["reading_order_true"] = 0
        normalized = normalize_page(page)
        artifact = build_candidate_edges(normalized)
        self.assertEqual(len(artifact["edges"]), 1)
        self.assertFalse(artifact["degraded"]["active"])
        edge = artifact["edges"][0]
        self.assertEqual(edge["source_block_id"], edge["target_block_id"])

    def test_equal_geometry_candidates_tie_break_by_target_id(self) -> None:
        page = {
            "page_id": "tie",
            "canvas_shape": [100, 100],
            "blocks": [
                {
                    "block_id": block_id,
                    "x1": 10,
                    "y1": 10,
                    "x2": 40,
                    "y2": 20,
                    "block_type": "body",
                    "text_len": 5,
                    "reading_order_true": block_id - 1,
                }
                for block_id in (3, 1, 2)
            ],
        }
        edges = build_candidate_edges(normalize_page(page))["edges"]
        for source in (1, 2, 3):
            targets = [
                edge["target_block_id"]
                for edge in edges
                if edge["source_block_id"] == source
            ]
            self.assertEqual(targets, sorted(targets))

    def test_missing_coverage_raises_stable_code(self) -> None:
        normalized = normalize_page(generate_pages()[0])
        with self.assertRaises(LayoutError) as context:
            validate_candidate_coverage(normalized, [])
        self.assertEqual(context.exception.code, "E_BLOCK_COVERAGE")
        self.assertEqual(context.exception.block_id, 1)

    def test_resource_limits_error_and_degrade(self) -> None:
        page = generate_pages()[0]
        normalized = normalize_page(page)
        with self.assertRaises(LayoutError) as context:
            build_candidate_edges(
                normalized,
                limits={
                    "max_blocks_per_page": 2,
                    "max_edge_comparisons": 100,
                    "max_resident_memory_mb": 64,
                    "limit_policy": "error",
                },
            )
        self.assertEqual(context.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(context.exception.field, "limits.max_blocks_per_page")
        degraded = build_candidate_edges(
            normalized,
            limits={
                "max_blocks_per_page": 14,
                "max_edge_comparisons": 1,
                "max_resident_memory_mb": 64,
                "limit_policy": "degrade",
            },
        )
        self.assertTrue(degraded["degraded"]["active"])
        self.assertEqual(
            degraded["degraded"]["category"], "edge_comparison_budget"
        )
        memory_degraded = build_candidate_edges(
            normalized,
            limits={
                "max_blocks_per_page": 14,
                "max_edge_comparisons": 100_000,
                "max_resident_memory_mb": 0.01,
                "limit_policy": "degrade",
            },
        )
        self.assertEqual(
            memory_degraded["degraded"]["category"], "memory_budget"
        )

    def test_out_of_range_true_order_is_rejected(self) -> None:
        page = generate_pages()[0]
        page["blocks"][0]["reading_order_true"] = 999
        with self.assertRaises(LayoutError) as context:
            normalize_page(page)
        self.assertEqual(context.exception.code, "E_FORMAT_INVALID")
        self.assertEqual(context.exception.field, "reading_order_true")


if __name__ == "__main__":
    unittest.main()

