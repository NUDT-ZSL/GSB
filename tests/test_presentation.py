import unittest

from drift_workbench.engine.graph import DependencyGraph
from drift_workbench.engine.models import Drift
from drift_workbench.gui.presentation import (
    HEAT_COLORS,
    heat_index,
    stable_node_layout,
    summarize_drifts,
)


class PresentationTests(unittest.TestCase):
    def test_heat_index_priority(self):
        drifts = [
            Drift("a", "missing", "*", {"x": 1}, None),
            Drift("b", "value_mismatch", "p", 1, 2),
            Drift("c", "extra", "*", None, {"q": 1}),
        ]
        severity_a, kind_a, color_a = heat_index("a", drifts, set())
        self.assertEqual((severity_a, kind_a), (3, "missing"))
        self.assertEqual(color_a, HEAT_COLORS["missing"])
        severity_b, kind_b, _ = heat_index("b", drifts, set())
        self.assertEqual((severity_b, kind_b), (2, "value_mismatch"))
        severity_c, kind_c, _ = heat_index("c", drifts, set())
        self.assertEqual((severity_c, kind_c), (1, "extra"))
        severity_d, kind_d, _ = heat_index("d", drifts, {"d"})
        self.assertEqual((severity_d, kind_d), (1, "artifact_invalid"))

    def test_layout_is_deterministic(self):
        nodes = ["z", "a", "m"]
        first = stable_node_layout(nodes)
        second = stable_node_layout(nodes)
        self.assertEqual(first, second)
        self.assertEqual(list(first), sorted(nodes))

    def test_summary_counts(self):
        drifts = [
            Drift("a", "missing", "*", None, None),
            Drift("b", "missing", "x", None, None),
            Drift("c", "value_mismatch", "y", 1, 2),
        ]
        self.assertEqual(
            summarize_drifts(drifts),
            {
                "missing": 2,
                "value_mismatch": 1,
                "extra": 0,
                "artifact_invalid": 0,
            },
        )

    def test_graph_topological_and_propagation(self):
        graph = DependencyGraph({("a", "b"), ("b", "c"), ("a", "c")})
        self.assertEqual(graph.topological_order(), ["a", "b", "c"])
        self.assertEqual(graph.downstream({"a"}), ("b", "c"))
        paths = graph.propagation_paths({"a"})
        # BFS explores successors in sorted order; direct edge a->c is the
        # shortest deterministic propagation path.
        self.assertEqual(paths["b"], ("a", "b"))
        self.assertEqual(paths["c"], ("a", "c"))


if __name__ == "__main__":
    unittest.main()
