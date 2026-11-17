from _bootstrap import ROOT  # noqa: F401
import unittest

from incrattn.dependency import DependencyGraph


class DependencyTest(unittest.TestCase):
    def setUp(self):
        self.graph = DependencyGraph.build(4, 10, "cfg")

    def test_causal_read_intervals(self):
        self.assertEqual(
            [node.kv_block_range for node in self.graph.nodes],
            [(0, 1), (0, 2), (0, 3)],
        )

    def test_readers_and_downstream_closure(self):
        self.assertEqual(self.graph.readers_of(1), [1, 2])
        invalidated = self.graph.invalidate_interval(1, 2)
        self.assertEqual(invalidated, [1, 2])
        self.assertTrue(self.graph.nodes[0].valid is False)
        for node in self.graph.nodes[1:]:
            self.assertFalse(node.valid)
            self.assertIsNone(node.output_fingerprint)

    def test_earliest_block_invalidation_keeps_unrelated_block_reusable(self):
        for node in self.graph.nodes:
            node.valid = True
        invalidated = self.graph.invalidate_interval(2, 3)
        self.assertEqual(invalidated, [2])
        self.assertTrue(self.graph.nodes[0].valid)
        self.assertTrue(self.graph.nodes[1].valid)
        self.assertFalse(self.graph.nodes[2].valid)

    def test_invalidate_all(self):
        self.assertEqual(self.graph.invalidate_all(), [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
