from _bootstrap import ROOT  # noqa: F401
import os
import tempfile
import unittest

import numpy as np

from incrattn.audit import audit_engine, verify_equivalence
from incrattn.config import EngineConfig
from incrattn.engine import IncrementalAttentionEngine


class EquivalenceTest(unittest.TestCase):
    def _engine(self, budget=2, workers=2, block_size=4):
        rng = np.random.default_rng(99)
        queries = np.round(rng.normal(size=(14, 5)), 3).astype(np.float32)
        path = os.path.join(tempfile.mkdtemp(), "snapshot.npz")
        config = EngineConfig(
            block_size=block_size,
            recompute_budget=budget,
            max_workers=workers,
            snapshot_path=path,
        )
        return IncrementalAttentionEngine(config, queries), rng

    def test_append_replace_and_full_recompute_are_equivalent(self):
        engine, rng = self._engine()
        chunks = [
            rng.normal(size=(4, 5)).astype(np.float32),
            rng.normal(size=(4, 5)).astype(np.float32),
            rng.normal(size=(4, 5)).astype(np.float32),
            rng.normal(size=(2, 5)).astype(np.float32),
        ]
        for block in chunks[:2]:
            engine.append_kv(block, block)
            engine.run_to_convergence()
        replacement = rng.normal(size=(4, 5)).astype(np.float32)
        engine.replace_kv_block(1, replacement, replacement)
        engine.run_to_convergence()
        for block in chunks[2:]:
            engine.append_kv(block, block)
            engine.run_to_convergence()

        equivalence = verify_equivalence(engine)
        self.assertTrue(equivalence["equivalent"], equivalence["mismatches"])
        audit = audit_engine(engine)
        self.assertTrue(audit["all_within_tolerance"])

    def test_block_size_change_rebuilds_accumulation_chain(self):
        engine, rng = self._engine(block_size=4)
        for rows in (4, 4, 4, 2):
            block = rng.normal(size=(rows, 5)).astype(np.float32)
            engine.append_kv(block, block)
        engine.run_to_convergence()
        old_fingerprints = {
            index: result.fingerprint
            for index, result in engine.state.results.items()
        }
        engine.change_block_size(3)
        self.assertEqual(len(engine.state.graph.nodes), 5)
        self.assertEqual(engine.state.results, {})
        engine.run_to_convergence()
        new_fingerprints = {
            index: result.fingerprint
            for index, result in engine.state.results.items()
        }
        self.assertEqual(set(new_fingerprints), set(range(5)))
        self.assertNotEqual(set(old_fingerprints.values()),
                            set(new_fingerprints.values()))
        equivalence = verify_equivalence(engine)
        self.assertTrue(equivalence["equivalent"])


if __name__ == "__main__":
    unittest.main()
