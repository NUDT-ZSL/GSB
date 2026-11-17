from _bootstrap import ROOT  # noqa: F401
import os
import tempfile
import unittest

import numpy as np

from incrattn.config import EngineConfig
from incrattn.engine import IncrementalAttentionEngine


def build_and_run(workers, seed=123):
    rng = np.random.default_rng(seed)
    queries = rng.normal(size=(13, 5)).astype(np.float32)
    config = EngineConfig(
        block_size=4,
        recompute_budget=2,
        max_workers=workers,
        snapshot_path=os.path.join(tempfile.mkdtemp(), f"d{workers}.npz"),
        scale=0.5,
    )
    engine = IncrementalAttentionEngine(config, queries)
    for rows in (4, 4, 4, 1):
        block = rng.normal(size=(rows, 5)).astype(np.float32)
        engine.append_kv(block, block)
    engine.run_to_convergence()
    return {
        index: (
            engine.state.results[index].output.copy(),
            engine.state.results[index].row_max.copy(),
            engine.state.results[index].exp_sum.copy(),
            engine.state.results[index].fingerprint,
        )
        for index in sorted(engine.state.results)
    }


class DeterminismTest(unittest.TestCase):
    def test_runs_are_bit_identical_across_workers_and_repeats(self):
        baseline = build_and_run(1)
        for workers in (1, 2, 4, 8):
            other = build_and_run(workers)
            self.assertEqual(set(baseline), set(other))
            for index in baseline:
                out_a, max_a, sum_a, fp_a = baseline[index]
                out_b, max_b, sum_b, fp_b = other[index]
                np.testing.assert_array_equal(out_a, out_b)
                np.testing.assert_array_equal(max_a, max_b)
                np.testing.assert_array_equal(sum_a, sum_b)
                self.assertEqual(fp_a, fp_b)


if __name__ == "__main__":
    unittest.main()
