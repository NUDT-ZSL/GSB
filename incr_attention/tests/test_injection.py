from _bootstrap import ROOT  # noqa: F401
import os
import tempfile
import unittest

import numpy as np

from incrattn.config import EngineConfig
from incrattn.engine import IncrementalAttentionEngine
from incrattn.errors import (
    E_KERNEL_NORMALIZE,
    E_KERNEL_SCORE,
    E_KERNEL_WEIGHTED,
    E_NON_FINITE_INPUT,
    InvalidConfigError,
)
from incrattn.injection import InjectionConfig


def setup(seed=31, budget=4):
    rng = np.random.default_rng(seed)
    queries = rng.normal(size=(12, 4)).astype(np.float32)
    path = os.path.join(tempfile.mkdtemp(), "inj.npz")
    config = EngineConfig(
        block_size=4,
        recompute_budget=budget,
        max_workers=2,
        snapshot_path=path,
        scale=0.5,
    )
    return rng, queries, config


class InjectionTest(unittest.TestCase):
    def test_stage_failures_are_isolated_and_reported(self):
        rng, queries, config = setup()
        items = [
            {"id": "INJ-006", "kind": "stage_raise", "stage": "score", "block_index": 1},
            {"id": "INJ-008", "kind": "stage_raise", "stage": "weighted", "block_index": 0},
        ]
        engine = IncrementalAttentionEngine(
            config, queries, InjectionConfig(items)
        )
        for rows in (4, 4, 4):
            block = rng.normal(size=(rows, 4)).astype(np.float32)
            engine.append_kv(block, block)
        report = engine.run_batch()
        self.assertEqual(
            sorted(report["injections_fired"]), ["INJ-006", "INJ-008"]
        )
        codes = {item["block_index"]: item["code"] for item in report["failures"]}
        self.assertEqual(codes[0], E_KERNEL_WEIGHTED)
        self.assertEqual(codes[1], E_KERNEL_SCORE)
        self.assertNotIn(0, engine.state.results)
        self.assertIn(2, engine.state.results)
        self.assertFalse(engine.state.graph.nodes[0].valid)

    def test_nonfinite_input_is_a_block_failure(self):
        rng, queries, config = setup()
        engine = IncrementalAttentionEngine(config, queries)
        block = rng.normal(size=(4, 4)).astype(np.float32)
        block[0, 0] = np.inf
        engine.append_kv(block, block)
        report = engine.run_batch()
        self.assertEqual(report["failures"][0]["code"], E_NON_FINITE_INPUT)
        self.assertEqual(report["failures"][0]["stage"], "input")

    def test_zero_block_size_rejected(self):
        _, queries, config = setup()
        with self.assertRaises(InvalidConfigError):
            IncrementalAttentionEngine(config.replace(block_size=0), queries)

    def test_failed_block_does_not_reenter_reuse_set(self):
        rng, queries, config = setup(budget=2)
        item = [{"id": "INJ-007", "kind": "stage_raise", "stage": "normalize", "block_index": 0}]
        engine = IncrementalAttentionEngine(
            config, queries, InjectionConfig(item)
        )
        block = rng.normal(size=(4, 4)).astype(np.float32)
        engine.append_kv(block, block)
        first = engine.run_batch()
        self.assertEqual(first["failures"][0]["code"], E_KERNEL_NORMALIZE)
        self.assertEqual(first["reused"], [])
        self.assertIn(0, engine.state.pending)
        engine.injection = InjectionConfig([])
        second = engine.run_batch()
        # Block 0 now succeeds; the still-pending later block is processed in
        # the same batch up to the budget.
        self.assertIn(0, second["completed"])
        self.assertTrue(second["converged"])


if __name__ == "__main__":
    unittest.main()
