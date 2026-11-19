import unittest

from layout_order.config import AppConfig, CostWeights
from layout_order.errors import E_CONFIG_INVALID, E_INCONSISTENT, LayoutOrderError
from layout_order.edges import (
    FALLBACK_CHAIN,
    build_candidates,
    recompute_edge_cost,
)
from layout_order.generator import generate_page
from layout_order.geometry import normalize_page, parse_page


def _features(family: str = "two_column", n: int = 14, seed: int = 5):
    record = generate_page(family, n, seed, "edge_page")
    return normalize_page(parse_page(record))


class CandidateEdgeTests(unittest.TestCase):
    def test_candidate_set_byte_identical_and_canonically_ordered(self) -> None:
        features = _features()
        config = AppConfig()
        first = build_candidates(features, config.weights)
        second = build_candidates(features, config.weights)
        self.assertEqual(
            [edge.to_dict() for edge in first.edges],
            [edge.to_dict() for edge in second.edges],
        )
        keys = [(edge.src, edge.dst) for edge in first.edges]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(
            [edge.edge_id for edge in first.edges],
            [f"e{index:06d}" for index in range(len(first.edges))],
        )

    def test_fallback_guarantees_hamiltonian_feasibility(self) -> None:
        for family in ("single_column", "two_column", "three_column", "mixed_regions"):
            features = _features(family, n=12, seed=3)
            graph = build_candidates(features, AppConfig().weights)
            ids = sorted(f.block_id for f in features)
            # 任意全局排序的相邻块对都应有候选边（兜底全连接双向保证）。
            edge_pairs = {(edge.src, edge.dst) for edge in graph.edges}
            for src, dst in zip(ids, ids[1:]):
                self.assertIn((src, dst), edge_pairs)
            self.assertTrue(all(FALLBACK_CHAIN in edge.flags for edge in graph.edges[-2:]))

    def test_edge_terms_independently_recompute_to_total(self) -> None:
        features = _features("mixed_regions", n=18)
        graph = build_candidates(features, AppConfig().weights)
        for edge in graph.edges:
            recomputed = recompute_edge_cost(edge, features, AppConfig().weights)
            self.assertEqual(recomputed, edge.total_cost)
            term_sum = round(sum(edge.terms.values()), 9)
            self.assertEqual(term_sum, edge.total_cost)

    def test_cross_column_ratio_in_unit_interval(self) -> None:
        features = _features("three_column", n=20)
        graph = build_candidates(features, AppConfig().weights)
        self.assertGreaterEqual(graph.cross_column_ratio, 0.0)
        self.assertLessEqual(graph.cross_column_ratio, 1.0)


class CostConfigTests(unittest.TestCase):
    def test_invalid_weight_returns_config_invalid_with_field(self) -> None:
        weights = CostWeights(w_gap=-1.0)
        with self.assertRaises(LayoutOrderError) as caught:
            weights.validate()
        self.assertEqual(caught.exception.code, E_CONFIG_INVALID)
        self.assertEqual(caught.exception.details["field"], "w_gap")

    def test_non_finite_weight_rejected(self) -> None:
        weights = CostWeights(w_type=float("nan"))
        with self.assertRaises(LayoutOrderError) as caught:
            weights.validate()
        self.assertEqual(caught.exception.code, E_CONFIG_INVALID)
        self.assertEqual(caught.exception.details["field"], "w_type")

    def test_unknown_type_in_forbidden_transitions_rejected(self) -> None:
        weights = CostWeights(forbidden_transitions={"footer": ["ghost"]})
        with self.assertRaises(LayoutOrderError) as caught:
            weights.validate()
        self.assertEqual(caught.exception.code, E_CONFIG_INVALID)
        self.assertEqual(caught.exception.details["field"], "forbidden_transitions")

    def test_tampered_terms_detected_by_recompute(self) -> None:
        features = _features()
        graph = build_candidates(features, AppConfig().weights)
        edge = graph.edges[0]
        tampered_terms = dict(edge.terms)
        tampered_terms["gap"] = edge.terms["gap"] + 0.123
        tampered = type(edge)(
            edge_id=edge.edge_id,
            src=edge.src,
            dst=edge.dst,
            priority=edge.priority,
            flags=edge.flags,
            same_column=edge.same_column,
            cross_region=edge.cross_region,
            upward=edge.upward,
            vertical_gap=edge.vertical_gap,
            horizontal_overlap=edge.horizontal_overlap,
            terms=tampered_terms,
            total_cost=edge.total_cost,
        )
        with self.assertRaises(LayoutOrderError) as caught:
            recompute_edge_cost(tampered, features, AppConfig().weights)
        self.assertEqual(caught.exception.code, E_INCONSISTENT)
        self.assertEqual(caught.exception.details["category"], "edge_terms_mismatch")


if __name__ == "__main__":
    unittest.main()
