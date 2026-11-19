import unittest

from layout_order.config import AppConfig, ResourceLimits, SolverPolicy
from layout_order.edges import build_candidates
from layout_order.errors import (
    E_ACCURACY_FLOOR,
    E_ORDER_INCONSISTENT,
    E_RESOURCE_LIMIT,
    LayoutOrderError,
)
from layout_order.generator import generate_page
from layout_order.geometry import normalize_page, parse_page
from layout_order.metrics import evaluate_page, ground_truth_order
from layout_order.selector import BANDED, EXACT, GREEDY, decide
from layout_order.solvers import solve_banded, solve_exact, solve_greedy


def _page(family: str, n: int, seed: int):
    record = generate_page(family, n, seed, "solver_page")
    page = parse_page(record)
    features = normalize_page(page)
    return page, features


class SolverTests(unittest.TestCase):
    def test_exact_solver_recovers_truth_on_small_pages(self) -> None:
        config = AppConfig()
        for family, n, seed in [
            ("single_column", 8, 1),
            ("two_column", 12, 2),
            ("mixed_regions", 11, 3),
        ]:
            _, features = _page(family, n, seed)
            graph = build_candidates(features, config.weights)
            result = solve_exact(features, graph)
            self.assertEqual(result.order, ground_truth_order(features))
            self.assertEqual(len(set(result.order)), n)

    def test_exact_is_no_worse_than_any_candidate_chain(self) -> None:
        config = AppConfig()
        _, features = _page("two_column", 9, 17)
        graph = build_candidates(features, config.weights)
        result = solve_exact(features, graph)
        lookup = graph.cost_lookup()
        truth_cost = round(
            sum(lookup[pair] for pair in zip(ground_truth_order(features),
                                             ground_truth_order(features)[1:])),
            9,
        )
        self.assertLessEqual(result.total_cost, truth_cost + 1e-9)

    def test_banded_and_greedy_cover_all_blocks_with_candidate_edges(self) -> None:
        config = AppConfig()
        _, features = _page("three_column", 30, 9)
        graph = build_candidates(features, config.weights)
        for result in (
            solve_banded(features, graph, config.policy.band_max_subset),
            solve_greedy(features, graph),
        ):
            self.assertEqual(sorted(result.order), sorted(f.block_id for f in features))
            edge_pairs = {(edge.src, edge.dst) for edge in graph.edges}
            self.assertTrue(
                all(pair in edge_pairs for pair in zip(result.order, result.order[1:]))
            )

    def test_chain_validation_flags_missing_and_duplicates(self) -> None:
        config = AppConfig()
        _, features = _page("single_column", 6, 4)
        graph = build_candidates(features, config.weights)
        ids = [f.block_id for f in features]
        broken = ids[:-1] + [ids[0]]
        from layout_order.solvers import validate_chain

        with self.assertRaises(LayoutOrderError) as caught:
            validate_chain(broken, ids, graph, round_cost=None)
        self.assertEqual(caught.exception.code, E_ORDER_INCONSISTENT)
        self.assertEqual(caught.exception.details["category"], "chain_coverage")
        self.assertIn(ids[-1], caught.exception.details["missing"])
        self.assertIn(ids[0], caught.exception.details["duplicates"])


class SelectorTests(unittest.TestCase):
    def test_decisions_are_deterministic_and_path_aligned(self) -> None:
        config = AppConfig()
        _, features = _page("two_column", 24, 21)
        graph = build_candidates(features, config.weights)
        first = decide(features, graph, config).to_dict()
        second = decide(features, graph, config).to_dict()
        self.assertEqual(first, second)

    def test_scale_transitions_exact_banded_greedy(self) -> None:
        config = AppConfig()
        chosen = []
        for n, family, seed in [
            (8, "single_column", 1),
            (24, "two_column", 2),
            (60, "three_column", 6),
        ]:
            _, features = _page(family, n, seed)
            graph = build_candidates(features, config.weights)
            chosen.append(decide(features, graph, config).selected_path)
        self.assertEqual(chosen, [EXACT, BANDED, GREEDY])

    def test_tight_edge_limit_raises_resource_limit(self) -> None:
        config = AppConfig(
            limits=ResourceLimits(max_edges=10, max_comparisons=10**9,
                                  max_resident_bytes=10**9)
        )
        _, features = _page("two_column", 12, 8)
        with self.assertRaises(LayoutOrderError) as caught:
            build_candidates(features, config.weights, max_edges=10)
        self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(caught.exception.details["category"], "max_edges")

    def test_greedy_path_blocked_by_tight_comparison_limit(self) -> None:
        config = AppConfig(
            policy=SolverPolicy(
                exact_max_blocks=2,
                band_max_blocks=3,
                exact_compare_budget=1,
                band_compare_budget=1,
            ),
            limits=ResourceLimits(
                max_edges=20_000,
                max_comparisons=1,
                max_resident_bytes=10**9,
            ),
        )
        _, features = _page("two_column", 10, 8)
        graph = build_candidates(features, config.weights)
        with self.assertRaises(LayoutOrderError) as caught:
            decide(features, graph, config)
        self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(caught.exception.details["category"], "all_paths_exceed_limits")

    def test_accuracy_floor_marked_for_greedy_degradation(self) -> None:
        strict_policy = SolverPolicy(
            accuracy_floor=0.99,
            band_max_blocks=40,
            band_compare_budget=30_000_000,
        )
        config = AppConfig(policy=strict_policy)
        _, features = _page("three_column", 60, 6)
        graph = build_candidates(features, config.weights)
        decision = decide(features, graph, config)
        self.assertEqual(decision.selected_path, GREEDY)
        metrics = evaluate_page(features, solve_greedy(features, graph).order, config)
        if metrics.adjacent_pair_accuracy < 0.99:
            self.assertLess(metrics.adjacent_pair_accuracy, 0.99)
        self.assertTrue(decision.degraded)


if __name__ == "__main__":
    unittest.main()
