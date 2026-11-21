from __future__ import annotations

import itertools
import unittest

from layout_order.canonical import canonical_json
from layout_order.costs import (
    DEFAULT_COST_CONFIG,
    normalize_cost_config,
    recompute_total,
    score_edges,
    validate_cost_details,
)
from layout_order.errors import LayoutError
from layout_order.geometry import build_candidate_edges, normalize_page
from layout_order.samples import generate_pages
from layout_order.solver import solve_order, validate_order


class CostAndSolverTests(unittest.TestCase):
    def test_costs_are_deterministic_and_recomputable(self) -> None:
        page = generate_pages()[2]
        normalized = normalize_page(page)
        candidate_edges = build_candidate_edges(normalized)["edges"]
        first = score_edges(normalized, candidate_edges)
        second = score_edges(normalized, candidate_edges)
        self.assertEqual(canonical_json(first), canonical_json(second))
        validate_cost_details(normalized, candidate_edges, first)
        for edge in first:
            self.assertAlmostEqual(
                recompute_total(edge), edge["total_cost"], places=8
            )
            self.assertEqual(
                set(edge["components"]),
                {
                    "type_order_violation",
                    "column_transition",
                    "vertical_gap",
                    "horizontal_misalignment",
                    "cross_zone",
                },
            )

    def test_invalid_weight_reports_field(self) -> None:
        invalid = {
            "weights": dict(DEFAULT_COST_CONFIG["weights"]),
            "type_order": dict(DEFAULT_COST_CONFIG["type_order"]),
            "type_violation_penalty": 10.0,
            "precision": 8,
        }
        invalid["weights"]["vertical_gap"] = -2
        with self.assertRaises(LayoutError) as context:
            normalize_cost_config(invalid)
        self.assertEqual(context.exception.code, "E_CONFIG_INVALID")
        self.assertEqual(context.exception.field, "weights.vertical_gap")

    def test_exact_optimum_matches_bruteforce(self) -> None:
        page = generate_pages()[0]
        page["blocks"] = page["blocks"][:6]
        for index, block in enumerate(page["blocks"]):
            block["block_id"] = index + 1
            block["reading_order_true"] = index
        normalized = normalize_page(page)
        candidates = build_candidate_edges(normalized)["edges"]
        scored = score_edges(normalized, candidates)
        lookup = {
            (edge["source_block_id"], edge["target_block_id"]): edge["total_cost"]
            for edge in scored
        }
        best: tuple[float, tuple[int, ...]] | None = None
        for permutation in itertools.permutations(range(1, 7)):
            cost = sum(
                lookup[(left, right)]
                for left, right in zip(permutation, permutation[1:])
            )
            candidate = (round(cost, 8), permutation)
            if best is None or candidate < best:
                best = candidate
        result = solve_order(normalized, scored)
        self.assertAlmostEqual(result["total_cost"], best[0], places=8)
        self.assertEqual(tuple(result["sequence"]), best[1])

    def test_uniform_cost_ties_choose_lexicographic_sequence(self) -> None:
        page = generate_pages()[0]
        page["blocks"] = page["blocks"][:4]
        for index, block in enumerate(page["blocks"]):
            block["block_id"] = index + 1
            block["reading_order_true"] = index
        normalized = normalize_page(page)
        candidates = build_candidate_edges(normalized)["edges"]
        scored = score_edges(normalized, candidates)
        for edge in scored:
            edge["components"] = {
                name: 0.0 for name in edge["components"]
            }
            edge["total_cost"] = 0.0
        result = solve_order(normalized, scored)
        self.assertEqual(result["sequence"], (1, 2, 3, 4))

    def test_order_validation_rejects_broken_chain(self) -> None:
        page = generate_pages()[0]
        normalized = normalize_page(page)
        scored = score_edges(
            normalized, build_candidate_edges(normalized)["edges"]
        )
        with self.assertRaises(LayoutError) as context:
            validate_order(normalized, [1, 2, 2, 4], scored)
        self.assertEqual(context.exception.code, "E_ORDER_INCONSISTENT")
        with self.assertRaises(LayoutError) as context:
            validate_order(normalized, [1, 2, 3], scored)
        self.assertEqual(context.exception.code, "E_ORDER_INCONSISTENT")

    def test_no_hamiltonian_path_reports_block(self) -> None:
        page = {
            "page_id": "no-path",
            "canvas_shape": [100, 100],
            "blocks": [
                {
                    "block_id": block_id,
                    "x1": 10,
                    "y1": block_id * 10,
                    "x2": 40,
                    "y2": block_id * 10 + 5,
                    "block_type": "body",
                    "text_len": 4,
                    "reading_order_true": block_id - 1,
                }
                for block_id in (1, 2, 3)
            ],
        }
        normalized = normalize_page(page)

        def edge(source: int, target: int) -> dict:
            return {
                "source_block_id": source,
                "target_block_id": target,
                "components": {
                    "type_order_violation": 0.0,
                    "column_transition": 0.0,
                    "vertical_gap": 1.0,
                    "horizontal_misalignment": 0.0,
                    "cross_zone": 0.0,
                },
                "total_cost": 1.0,
                "candidate_rank": 0,
                "features": {
                    "same_column": True,
                    "cross_column": False,
                    "cross_zone": False,
                    "vertical_adjacent": True,
                    "target_below": True,
                    "backward": False,
                    "horizontal_overlap": 1.0,
                    "vertical_gap": 1.0,
                    "dx": 0.0,
                    "dy": 1.0,
                },
            }

        with self.assertRaises(LayoutError) as context:
            solve_order(normalized, [edge(1, 2), edge(2, 1)])
        self.assertEqual(context.exception.code, "E_ORDER_INCONSISTENT")
        self.assertEqual(context.exception.block_id, 3)


if __name__ == "__main__":
    unittest.main()

