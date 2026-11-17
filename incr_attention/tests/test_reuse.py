from _bootstrap import ROOT  # noqa: F401
import os
import tempfile
import unittest

import numpy as np

from incrattn.config import EngineConfig
from incrattn.engine import IncrementalAttentionEngine


class ReuseTest(unittest.TestCase):
    def test_unaffected_blocks_are_reused_without_kernel_rerun(self):
        rng = np.random.default_rng(88)
        queries = rng.normal(size=(12, 4)).astype(np.float32)
        config = EngineConfig(
            block_size=4,
            recompute_budget=4,
            max_workers=1,
            snapshot_path=os.path.join(tempfile.mkdtemp(), "reuse.npz"),
            scale=0.5,
        )
        engine = IncrementalAttentionEngine(config, queries)
        for rows in (4, 4, 4):
            block = rng.normal(size=(rows, 4)).astype(np.float32)
            engine.append_kv(block, block)
        engine.run_to_convergence()
        untouched = {
            index: engine.state.results[index].fingerprint
            for index in (0, 1)
        }

        replacement = rng.normal(size=(4, 4)).astype(np.float32)
        engine.replace_kv_block(2, replacement, replacement)
        report = engine.run_batch()
        self.assertEqual(report["planned"], [2])
        self.assertEqual(report["reused"], [0, 1])
        for index in (0, 1):
            self.assertEqual(
                engine.state.results[index].fingerprint, untouched[index]
            )


if __name__ == "__main__":
    unittest.main()
