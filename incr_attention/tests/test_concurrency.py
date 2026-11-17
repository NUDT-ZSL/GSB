from _bootstrap import ROOT  # noqa: F401
import os
import tempfile
import unittest

import numpy as np

from incrattn.config import EngineConfig
from incrattn.engine import IncrementalAttentionEngine


def run_once(workers, budget, seed=55):
    rng = np.random.default_rng(seed)
    queries = rng.normal(size=(12, 4)).astype(np.float32)
    path = os.path.join(tempfile.mkdtemp(), f"w{workers}.npz")
    config = EngineConfig(
        block_size=4,
        recompute_budget=budget,
        max_workers=workers,
        snapshot_path=path,
        scale=0.5,
    )
    engine = IncrementalAttentionEngine(config, queries)
    for rows in (4, 4, 4):
        block = rng.normal(size=(rows, 4)).astype(np.float32)
        engine.append_kv(block, block)
    reports = engine.run_to_convergence()
    ordered = [
        (
            tuple(report["planned"]),
            tuple(report["completed"]),
            tuple(item["block_index"] for item in report["pending"]),
            tuple(item["block_index"] for item in report["failures"]),
            report["budget_consumed"],
        )
        for report in reports
    ]
    fingerprints = [
        engine.state.results[index].fingerprint
        for index in sorted(engine.state.results)
    ]
    outputs = [
        engine.state.results[index].output.copy()
        for index in sorted(engine.state.results)
    ]
    return ordered, fingerprints, outputs


class ConcurrencyTest(unittest.TestCase):
    def test_serial_and_threadpool_orders_are_identical(self):
        serial_order, serial_fp, serial_out = run_once(1, 2)
        parallel_order, parallel_fp, parallel_out = run_once(4, 2)
        self.assertEqual(serial_order, parallel_order)
        self.assertEqual(serial_fp, parallel_fp)
        for left, right in zip(serial_out, parallel_out):
            np.testing.assert_array_equal(left, right)

    def test_stable_sort_keys_hold_under_high_concurrency(self):
        order_one, _, _ = run_once(2, 3)
        order_many, _, _ = run_once(8, 3)
        self.assertEqual(order_one, order_many)


if __name__ == "__main__":
    unittest.main()
