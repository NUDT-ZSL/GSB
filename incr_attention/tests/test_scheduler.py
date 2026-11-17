from _bootstrap import ROOT  # noqa: F401
import os
import tempfile
import unittest

import numpy as np

from incrattn.config import EngineConfig
from incrattn.engine import IncrementalAttentionEngine


def make_engine(budget, workers=1, block_size=4):
    rng = np.random.default_rng(11)
    queries = rng.normal(size=(10, 4)).astype(np.float32)
    path = os.path.join(tempfile.mkdtemp(), "snapshot.npz")
    config = EngineConfig(
        block_size=block_size,
        recompute_budget=budget,
        max_workers=workers,
        snapshot_path=path,
        scale=0.5,
    )
    return IncrementalAttentionEngine(config, queries), queries


class SchedulerTest(unittest.TestCase):
    def test_budget_keeps_stable_pending_queue(self):
        engine, _ = make_engine(budget=1)
        rng = np.random.default_rng(12)
        first_block = rng.normal(size=(4, 4)).astype(np.float32)
        invalidated = engine.append_kv(first_block, first_block)
        # A fresh KV interval 0 invalidates every output block that reads it.
        self.assertEqual(invalidated, [0, 1, 2])
        first = engine.run_batch()
        self.assertEqual(first["planned"], [0])
        self.assertEqual(
            [item["block_index"] for item in first["pending"]], [1, 2]
        )
        second = engine.run_batch()
        self.assertEqual(second["planned"], [1])
        self.assertEqual(
            [item["block_index"] for item in second["pending"]], [2]
        )
        third = engine.run_batch()
        self.assertEqual(third["planned"], [2])
        self.assertTrue(third["converged"])

        # A later append only invalidates readers of the new KV interval;
        # earlier completed blocks are reused and never rerun.
        next_block = rng.normal(size=(4, 4)).astype(np.float32)
        affected = engine.append_kv(next_block, next_block)
        self.assertEqual(affected, [1, 2])
        report = engine.run_batch()
        self.assertEqual(report["planned"], [1])
        self.assertIn(0, report["reused"])
        self.assertEqual(
            [item["block_index"] for item in report["pending"]], [2]
        )

    def test_earliest_invalid_block_is_never_skipped(self):
        engine, _ = make_engine(budget=1)
        rng = np.random.default_rng(3)
        remaining = 10
        while remaining:
            rows = min(4, remaining)
            block = rng.normal(size=(rows, 4)).astype(np.float32)
            engine.append_kv(block, block)
            remaining -= rows
        seen = []
        while engine.state.pending:
            report = engine.run_batch()
            seen.extend(report["planned"])
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen, list(range(3)))

    def test_zero_budget_marks_everything_pending(self):
        from incrattn.injection import InjectionConfig

        path = os.path.join(tempfile.mkdtemp(), "zero.npz")
        config = EngineConfig(
            block_size=4,
            recompute_budget=0,
            max_workers=1,
            snapshot_path=path,
            scale=0.5,
        )
        injection = InjectionConfig([
            {"id": "INJ-004", "kind": "zero_budget"}
        ])
        rng = np.random.default_rng(4)
        queries = rng.normal(size=(10, 4)).astype(np.float32)
        engine = IncrementalAttentionEngine(config, queries, injection=injection)
        block = rng.normal(size=(4, 4)).astype(np.float32)
        engine.append_kv(block, block)
        report = engine.run_batch()
        self.assertEqual(report["budget_consumed"], 0)
        self.assertEqual(
            [item["block_index"] for item in report["pending"]], [0, 1, 2]
        )
        self.assertEqual(report["pending"][0]["code"], "E_RECOMPUTE_PENDING")


if __name__ == "__main__":
    unittest.main()
