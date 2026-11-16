"""转移解析核心单元测试。"""

import unittest

import numpy as np

from dp_bench.errors import StepLimitError
from dp_bench.model import ACTIONS, Token, TransitionModel
from dp_bench.parser import (FINISH, LEFT_REDUCE, RIGHT_REDUCE, SHIFT,
                             DependencyParser)


def make_model(dim: int = 32) -> TransitionModel:
    rng = np.random.default_rng(7)
    params = {
        "feature_dim": dim,
        "hash_buckets": dim,
        "word_hash": {"<NULL>": 0, "<ROOT>": 1, "猫": 2, "追": 3, "狗": 4},
        "pos_hash": {"<NULL>": 0, "ROOT": 1, "NN": 2, "VV": 3},
        "weights": {a: rng.normal(0, 1, dim).tolist() for a in ACTIONS},
    }
    return TransitionModel(params)


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.model = make_model()
        self.tokens = [Token(1, "猫", "NN"), Token(2, "追", "VV"),
                       Token(3, "狗", "NN")]

    def test_finish_produces_arcs_and_log_score(self):
        parser = DependencyParser(self.model, step_limit=64, quota=64)
        result = parser.parse("S1", self.tokens)
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.code)
        self.assertEqual(result.steps_used, len(result.trace))
        # 三个词各一条弧，覆盖全部依存词 1..3
        self.assertEqual(len(result.arcs), 3)
        deps = sorted(arc.dep for arc in result.arcs)
        self.assertEqual(deps, [1, 2, 3])
        self.assertTrue(all(arc.head in (0, 1, 2, 3) for arc in result.arcs))
        self.assertTrue(all(arc.relation in ("LEFT", "RIGHT")
                            for arc in result.arcs))
        self.assertAlmostEqual(
            result.log_score,
            sum(step["log_prob"] for step in result.trace), places=12)

    def test_trace_snapshots_and_sort_key(self):
        parser = DependencyParser(self.model, step_limit=64, quota=64)
        result = parser.parse("S7", self.tokens)
        for step in result.trace:
            self.assertEqual(step["sort_key"],
                             f"S7@{step['step_no']:08d}")
            self.assertIn(step["action"], ACTIONS)
            self.assertIsInstance(step["stack"], list)
            self.assertIsInstance(step["buffer"], list)
        self.assertEqual(result.trace[-1]["action"], FINISH)

    def test_step_limit_terminates_without_arcs(self):
        parser = DependencyParser(self.model, step_limit=2, quota=64)
        result = parser.parse("S1", self.tokens)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.code, "E_STEP_LIMIT")
        self.assertEqual(result.steps_used, 2)
        self.assertEqual(result.arcs, [])
        self.assertIsNone(result.log_score)
        self.assertTrue(result.exceeded_step_limit)
        self.assertEqual(result.failure["step_no"], 3)

    def test_tie_break_priority_order(self):
        scores = {SHIFT: 1.0, LEFT_REDUCE: 1.0,
                  RIGHT_REDUCE: 1.0, FINISH: 0.5}
        action, tie = DependencyParser._choose(
            scores, [SHIFT, LEFT_REDUCE, RIGHT_REDUCE])
        self.assertEqual(action, SHIFT)
        self.assertTrue(tie)

        scores = {SHIFT: 0.0, LEFT_REDUCE: 2.0,
                  RIGHT_REDUCE: 2.0, FINISH: 0.0}
        action, tie = DependencyParser._choose(
            scores, [LEFT_REDUCE, RIGHT_REDUCE])
        self.assertEqual(action, LEFT_REDUCE)
        self.assertTrue(tie)

    def test_legal_actions(self):
        parser = DependencyParser(self.model, step_limit=64, quota=64)
        root = Token(0, "<ROOT>", "ROOT")
        # 初始：只能移进
        self.assertEqual(
            parser._legal_actions([root], [self.tokens[0]]), [SHIFT])
        # 栈两个元素且缓冲非空、次栈顶为 ROOT：禁止左归约
        legal = parser._legal_actions(
            [root, self.tokens[0]], [self.tokens[1]])
        self.assertEqual(legal, [SHIFT, RIGHT_REDUCE])
        # 次栈顶为真实词时左归约恢复合法
        legal = parser._legal_actions(
            [self.tokens[0], self.tokens[1]], [self.tokens[2]])
        self.assertEqual(legal, [SHIFT, LEFT_REDUCE, RIGHT_REDUCE])
        # 缓冲空、栈只剩 ROOT：只能结束
        self.assertEqual(parser._legal_actions([root], []), [FINISH])


if __name__ == "__main__":
    unittest.main()
