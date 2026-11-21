from __future__ import annotations

import json
import unittest

from quant_diag.demo import _corrupt_snapshot_probe
from quant_diag.errors import E_SNAPSHOT_CORRUPT, E_STAGE_BUSY, BusinessError
from quant_diag.selfcheck import run_consistency_checks
from quant_diag.snapshots import SnapshotStore
from quant_diag.util import copy_json

from tests.helpers import Workspace


class SnapshotAndSelfcheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Workspace()
        self.engine = self.workspace.engine("runtime_snapshot")
        self.engine.run()
        self.snapshot = self.engine.latest_snapshot()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_snapshot_corruption_reports_stage_and_version(self) -> None:
        result = _corrupt_snapshot_probe(
            self.workspace.root / "runtime_snapshot",
            self.workspace.root / "runtime_corrupt",
        )
        self.assertTrue(result["raised"])
        self.assertEqual(result["error"]["code"], E_SNAPSHOT_CORRUPT)
        self.assertEqual(result["error"]["stage"], "layer_inference")
        self.assertEqual(result["error"]["version"], 4)

    def test_concurrent_commit_gets_stage_busy_immediately(self) -> None:
        store = SnapshotStore(self.workspace.root / "runtime_snapshot")
        with store.commit_guard():
            with self.assertRaises(BusinessError) as raised:
                store.commit(
                    stage="layer_inference",
                    model_tag="tiny-llm-lab",
                    manifest_digest="m",
                    config_digest="c",
                    layer_order=["layer_0"],
                    completed_layers=["layer_0"],
                    stages={},
                    dependency={
                        "affected_layers": [],
                        "reused_layers": [],
                        "rebuilt_layers": [],
                    },
                    result_digest="r",
                    degradations=[],
                    injections=[],
                )
        self.assertEqual(raised.exception.code, E_STAGE_BUSY)

    def test_selfcheck_detects_missing_quant_layer(self) -> None:
        tampered = copy_json(self.snapshot)
        del tampered["stages"]["layer_quantization"]["layers"]["layer_2"]
        with self.assertRaises(BusinessError) as raised:
            run_consistency_checks(tampered, self.engine.config)
        self.assertEqual(raised.exception.code, "E_INCONSISTENT")
        self.assertEqual(raised.exception.details["category"], "missing_quant_layer")

    def test_selfcheck_detects_dependency_overlap(self) -> None:
        tampered = copy_json(self.snapshot)
        stage = tampered["stages"]["layer_inference"]
        stage["reused_layers"] = ["layer_0"]
        stage["rebuilt_layers"] = ["layer_0", "layer_1", "layer_2", "layer_3"]
        with self.assertRaises(BusinessError) as raised:
            run_consistency_checks(tampered, self.engine.config)
        self.assertEqual(raised.exception.code, "E_INCONSISTENT")
        self.assertEqual(
            raised.exception.details["category"], "reuse_rebuild_overlap"
        )

    def test_partial_stage_is_refused(self) -> None:
        store = SnapshotStore(self.workspace.root / "runtime_snapshot")
        with self.assertRaises(BusinessError) as raised:
            store.commit(
                stage="layer_inference",
                model_tag="tiny-llm-lab",
                manifest_digest="m",
                config_digest="c",
                layer_order=["layer_0", "layer_1"],
                completed_layers=["layer_0"],
                stages={},
                dependency={
                    "affected_layers": ["layer_1"],
                    "reused_layers": ["layer_0"],
                    "rebuilt_layers": [],
                },
                result_digest="r",
                degradations=[],
                injections=[],
            )
        self.assertEqual(raised.exception.code, "E_SNAPSHOT_PARTIAL")


if __name__ == "__main__":
    unittest.main()
