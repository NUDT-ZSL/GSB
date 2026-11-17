from _bootstrap import ROOT  # noqa: F401
import json
import os
import tempfile
import unittest

import numpy as np

from incrattn.config import EngineConfig
from incrattn.engine import IncrementalAttentionEngine
from incrattn.errors import SnapshotStaleError
from incrattn.snapshot import load_snapshot, tamper_snapshot_version


class SnapshotTest(unittest.TestCase):
    def _build(self, tag):
        rng = np.random.default_rng(21)
        queries = rng.normal(size=(8, 3)).astype(np.float32)
        path = os.path.join(tempfile.mkdtemp(), f"{tag}.npz")
        config = EngineConfig(
            block_size=4,
            recompute_budget=2,
            max_workers=1,
            snapshot_path=path,
            scale=0.5,
        )
        engine = IncrementalAttentionEngine(config, queries)
        block = rng.normal(size=(4, 3)).astype(np.float32)
        engine.append_kv(block, block)
        engine.run_batch()
        return engine, config

    def test_snapshot_round_trip_restores_recovery_point(self):
        engine, config = self._build("a")
        version = engine.state.global_version
        restored = IncrementalAttentionEngine.restore(config)
        self.assertEqual(restored.state.global_version, version)
        self.assertEqual(
            restored.state.results[0].fingerprint,
            engine.state.results[0].fingerprint,
        )
        snapshot = load_snapshot(config.snapshot_path)
        self.assertEqual(snapshot["metadata"]["completed_blocks"], [0, 1])
        self.assertIn("version=", restored.state.last_recovery_point)

    def test_stale_config_fingerprint_rejected(self):
        _, config = self._build("b")
        changed = config.replace(recompute_budget=1)
        with self.assertRaises(SnapshotStaleError):
            IncrementalAttentionEngine.restore(changed)

    def test_tampered_version_is_rejected(self):
        engine, config = self._build("c")
        tamper_snapshot_version(config.snapshot_path, 424242)
        with self.assertRaises(SnapshotStaleError):
            IncrementalAttentionEngine.restore(config)

    def test_partial_temp_file_is_not_a_recovery_point(self):
        engine, config = self._build("d")
        leftovers = [
            name
            for name in os.listdir(os.path.dirname(config.snapshot_path))
            if name.startswith(".snapshot-")
        ]
        self.assertEqual(leftovers, [])
        self.assertTrue(os.path.exists(config.snapshot_path))


if __name__ == "__main__":
    unittest.main()
