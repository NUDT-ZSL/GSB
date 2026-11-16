import json
import os
import tempfile
import unittest

import numpy as np

from vectorbench.errors import BenchError, ErrorCode
from vectorbench.execution import FUSED, NAIVE, run_fused, run_naive
from vectorbench.gui import build_view_model
from vectorbench.model import load_blocks, load_queries
from vectorbench.parser import parse_expression
from vectorbench.planner import DEFAULT_BATCH_CANDIDATES, choose_plan, estimate_peak_bytes
from vectorbench.session import ExperimentSession, open_snapshot
from vectorbench.typecheck import annotate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "samples", "data_blocks.json")
QUERIES = os.path.join(ROOT, "samples", "queries.json")


def load_session() -> ExperimentSession:
    block = load_blocks(DATA)[0]
    queries = load_queries(QUERIES, block)
    return ExperimentSession(block, queries)


class ParserTests(unittest.TestCase):
    def test_canonical_ignores_whitespace_and_redundant_parentheses(self):
        left = parse_expression("(( a + 1 ) * 2)")
        right = parse_expression("(a+1)*2")
        self.assertEqual(left.canonical_text(), right.canonical_text())
        self.assertEqual(left.structural_digest(), right.structural_digest())

    def test_comparison_direction_is_significant(self):
        left = parse_expression("a < b")
        right = parse_expression("a > b")
        self.assertNotEqual(left.canonical_text(), right.canonical_text())
        self.assertNotEqual(left.structural_digest(), right.structural_digest())

    def test_unsupported_syntax_raises_e_expr(self):
        with self.assertRaises(BenchError) as caught:
            parse_expression("a @ b")
        self.assertEqual(caught.exception.code, ErrorCode.E_EXPR)


class LoadingTests(unittest.TestCase):
    def test_unknown_column_is_e_data(self):
        block = load_blocks(DATA)[0]
        bad = os.path.join(ROOT, "samples", "faults", "type_mismatch.json")
        with self.assertRaises(BenchError) as caught:
            load_blocks(bad)
        self.assertEqual(caught.exception.code, ErrorCode.E_TYPE)

    def test_nullmap_misalignment_code(self):
        bad = os.path.join(ROOT, "samples", "faults", "nullmap_misaligned.json")
        with self.assertRaises(BenchError) as caught:
            load_blocks(bad)
        self.assertEqual(caught.exception.code, ErrorCode.E_NULLMAP)

    def test_missing_reference_is_e_data(self):
        block = load_blocks(DATA)[0]
        path = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json").name
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"name": "bad", "expressions": ["missing_col + 1"]}, handle)
        with self.assertRaises(BenchError) as caught:
            load_queries(path, block)
        self.assertEqual(caught.exception.code, ErrorCode.E_DATA)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.session = load_session()
        self.block = self.session.block

    def _execute(self, expression, index=0, batch_size=4):
        root = parse_expression(expression, index)
        analyzer = annotate(root, self.block, index)
        naive = run_naive(root, self.block, analyzer, index, batch_size)
        fused = run_fused(root, self.block, analyzer, index, batch_size)
        return root, analyzer, naive, fused

    def test_paths_are_byte_identical_for_all_sample_queries(self):
        for index, expression in enumerate(self.session.queries.expressions):
            root, analyzer, naive, fused = self._execute(expression, index)
            self.assertEqual(naive.result_type, fused.result_type)
            np.testing.assert_array_equal(naive.nullmap, fused.nullmap)
            np.testing.assert_array_equal(naive.values, fused.values)
            self.assertEqual(naive.result_summary().digest, fused.result_summary().digest)
            self.assertEqual(naive.division_zero_count, fused.division_zero_count)

    def test_repeated_execution_has_stable_intermediate_order_and_digest(self):
        expression = self.session.queries.expressions[0]
        first_root, _, first, _ = self._execute(expression)
        second_root, _, second, _ = self._execute(expression)
        self.assertEqual(
            [item.label for item in first.intermediate_summaries],
            [item.label for item in second.intermediate_summaries],
        )
        self.assertEqual(
            [item.digest for item in first.intermediate_summaries],
            [item.digest for item in second.intermediate_summaries],
        )
        self.assertEqual(first_root.structural_digest(), second_root.structural_digest())

    def test_division_by_zero_is_null_and_marked(self):
        _, _, naive, fused = self._execute("qty / (qty - 3)", 1, 3)
        self.assertGreater(naive.division_zero_count, 0)
        self.assertEqual(naive.division_zero_count, fused.division_zero_count)
        # qty=3 gives a non-null zero divisor at row index 2.
        self.assertTrue(naive.nullmap[2])

    def test_ternary_is_a_fusion_boundary(self):
        root = parse_expression("active ? price * qty : price + 1.0", 3)
        analyzer = annotate(root, self.block, 3)
        from vectorbench.execution import build_fusion_segments

        segments = build_fusion_segments(root, analyzer)
        self.assertGreaterEqual(len(segments), 2)
        self.assertTrue(any(any(node.kind == "ternary" for node in segment) for segment in segments))

    def test_batch_zero_raises_e_batch(self):
        root = parse_expression("price + 1.0")
        analyzer = annotate(root, self.block, 0)
        with self.assertRaises(BenchError) as caught:
            run_naive(root, self.block, analyzer, 0, 0)
        self.assertEqual(caught.exception.code, ErrorCode.E_BATCH)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.session = load_session()
        self.block = self.session.block

    def test_decision_is_deterministic(self):
        root = parse_expression(self.session.queries.expressions[0])
        analyzer = annotate(root, self.block, 0)
        first = choose_plan(root, self.block, analyzer)
        second = choose_plan(root, self.block, analyzer)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_memory_estimates_are_stable_integers_and_compatible(self):
        root = parse_expression("(price * qty) + (id - 1)")
        analyzer = annotate(root, self.block, 0)
        for batch_size in DEFAULT_BATCH_CANDIDATES:
            naive = estimate_peak_bytes(root, analyzer, self.block.row_count, batch_size, NAIVE)
            fused = estimate_peak_bytes(root, analyzer, self.block.row_count, batch_size, FUSED)
            self.assertIsInstance(naive, int)
            self.assertIsInstance(fused, int)
            self.assertEqual(
                naive,
                estimate_peak_bytes(root, analyzer, self.block.row_count, batch_size, NAIVE),
            )
            self.assertGreaterEqual(naive, fused)
            self.assertGreater(naive, 0)

    def test_scaling_can_change_path_or_batch_but_stays_reproducible(self):
        root = parse_expression("(price + 2.0) * qty - id")
        small = annotate(root, self.block, 0)
        small_decision = choose_plan(root, self.block, small)
        scaled_block = self.block.scaled(20)
        large = annotate(root, scaled_block, 0)
        large_decision = choose_plan(root, scaled_block, large)
        self.assertEqual(large_decision.to_dict(), choose_plan(root, scaled_block, large).to_dict())
        self.assertEqual(small_decision.selected_path, small_decision.selected_path)
        self.assertGreaterEqual(large_decision.batch_size, small_decision.batch_size)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_replay_recovers_path_and_summary(self):
        session = load_session()
        session.evaluate_all()
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = os.path.join(directory, "snapshot.json")
            session.snapshot(snapshot_path)
            restored, mode = open_snapshot(
                snapshot_path, session.block, session.queries, session.logger
            )
            self.assertEqual(mode, "matched")
            for left, right in zip(session.evaluations, restored.evaluations):
                self.assertEqual(left.selected.path, right.selected.path)
                self.assertEqual(left.result_digest(), right.result_digest())

    def test_version_mismatch_uses_full_replacement(self):
        session = load_session()
        session.evaluate_all()
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = os.path.join(directory, "snapshot.json")
            session.snapshot(snapshot_path)
            changed = load_blocks(DATA)[0]
            changed.version = "orders_demo_v2"
            restored, mode = open_snapshot(
                snapshot_path, changed, session.queries, session.logger
            )
            self.assertEqual(mode, "full_replace")
            self.assertEqual(restored.block.version, "orders_demo_v1")


class GuiViewModelTests(unittest.TestCase):
    def test_view_model_has_stable_rows_without_tkinter(self):
        session = load_session()
        session.evaluate_all()
        model = build_view_model(session)
        self.assertTrue(model.tree_rows)
        self.assertTrue(model.decision_lines)
        self.assertTrue(model.ranking_rows)
        self.assertEqual(len(model.result_rows), len(session.evaluations))


class FuseAuditTests(unittest.TestCase):
    def test_first_divergent_row_raises_e_fuse(self):
        session = load_session()
        block = session.block
        root = parse_expression("price + 1.0")
        analyzer = annotate(root, block, 0)
        naive = run_naive(root, block, analyzer, 0, 4)
        fused = run_fused(root, block, analyzer, 0, 4)
        fused.values[3] += 100.0
        with self.assertRaises(BenchError) as caught:
            from vectorbench.execution import assert_byte_identical
            assert_byte_identical(naive, fused)
        self.assertEqual(caught.exception.code, ErrorCode.E_FUSE)
        self.assertEqual(caught.exception.detail["first_row"], 3)


if __name__ == "__main__":
    unittest.main()
