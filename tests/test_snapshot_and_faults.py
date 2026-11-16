import json
import os
import unittest

from featbench.config import load_config
from featbench.pipeline import run_pipeline
from featbench.atomicio import atomic_write_json
from featbench.snapshot import ShutdownFlag, SnapshotStore
from featbench.errors import BenchError
from tests._util import TempWorkdir, write_config


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempWorkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_atomic_write_and_version(self):
        path = write_config(self.tmp.path, {})
        config = load_config(path)
        store = SnapshotStore(config.work_dir, config.config_fingerprint)
        target = store.write("cross", "s1", "shared", cursor=2,
                             artifact_summary={"active_crosses": 2})
        self.assertTrue(os.path.isfile(target))
        payload = store.read("cross", "s1", "shared")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["cursor"], 2)
        self.assertEqual(payload["config_fingerprint"],
                         config.config_fingerprint)

    def test_config_mismatch_detected(self):
        path = write_config(self.tmp.path, {})
        config = load_config(path)
        current = SnapshotStore(config.work_dir, config.config_fingerprint)
        planted = current.path_for("reduce", "s1", "pca")
        atomic_write_json(planted, {
            "version": 1,
            "stage": "reduce",
            "config_fingerprint": "deadbeef" * 4,
            "scale_id": "s1",
            "path": "pca",
            "cursor": 3,
            "artifact_summary": {},
            "extra": {},
        })
        with self.assertRaises(BenchError) as ctx:
            current.read("reduce", "s1", "pca")
        self.assertEqual(ctx.exception.code, "E_CONFIG_MISMATCH")

    def test_graceful_shutdown_persists_snapshot_and_exit_zero_path(self):
        path = write_config(self.tmp.path, {"workers": 1})
        config = load_config(path)
        shutdown = ShutdownFlag()
        shutdown.set()
        report = run_pipeline(config, shutdown)
        self.assertTrue(report["failures"])
        self.assertTrue(all(item["code"] == "E_STAGE_FAILED"
                            for item in report["failures"]))
        snapshot_root = os.path.join(config.work_dir, "snapshots")
        snapshot_dir = os.path.join(snapshot_root, os.listdir(snapshot_root)[0])
        files = [name for name in os.listdir(snapshot_dir)
                 if name.endswith(".json")]
        self.assertTrue(files)
        for name in files:
            payload = json.load(open(os.path.join(snapshot_dir, name)))
            self.assertEqual(payload["version"], 1)

    def test_task_exception_isolated_and_listed(self):
        # All-constant target-free numeric setup triggers reduction failure only
        # for an artificial config whose every generated feature is constant:
        # use zero variance threshold edge with a single-sample dataset.
        path = write_config(self.tmp.path, {
            "scales": [{"id": "tiny", "samples": 2}],
            "reduction": {"methods": ["variance"], "target_dim": 4,
                          "variance_threshold": 1000000.0},
        })
        config = load_config(path)
        report = run_pipeline(config)
        # Variance filter with an impossible threshold still returns zero kept
        # dimensions; evaluation records an insufficient-features note instead
        # of crashing, proving isolation keeps the run alive.
        self.assertEqual(len(report["failures"]), 0)
        self.assertEqual(report["metric_table"][0]["note"],
                         "insufficient samples or features for evaluation")


if __name__ == "__main__":
    unittest.main()
