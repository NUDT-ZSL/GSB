from __future__ import annotations

import unittest
from dataclasses import replace

from quant_diag.demo import _kill_worker
from quant_diag.manifest import ManifestView, load_manifest, write_manifest

from tests.helpers import Workspace


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Workspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_force_kill_discards_inflight_and_matches_full_run(self) -> None:
        baseline = self.workspace.engine("runtime_baseline")
        baseline_report = baseline.run()
        baseline_snapshot = baseline.latest_snapshot()
        recovery_runtime = self.workspace.root / "runtime_recovery"
        killed = _kill_worker(
            self.workspace.manifest,
            self.workspace.calibration,
            recovery_runtime,
            "layer_quantization",
            1,
        )
        self.assertEqual(killed.returncode, 137)
        resumed = self.workspace.engine("runtime_recovery")
        resumed_report = resumed.run()
        resumed_snapshot = resumed.latest_snapshot()
        self.assertEqual(
            baseline_snapshot["result_digest"], resumed_snapshot["result_digest"]
        )
        self.assertEqual(
            baseline_report["result_digest"], resumed_report["result_digest"]
        )
        recovery = resumed_report["recovery"]
        self.assertIsNotNone(recovery["discarded_uncommitted"])
        self.assertEqual(recovery["discarded_uncommitted"]["layer_id"], "layer_1")
        self.assertEqual(recovery["resume_stage"], "layer_quantization")
        self.assertEqual(recovery["resume_layer_index"], 0)
        actions = {item["stage"]: item for item in resumed_report["actions"]}
        self.assertEqual(actions["model_package_validation"]["action"], "reuse")
        self.assertEqual(actions["layer_quantization"]["action"], "commit")
        self.assertTrue(resumed_report["self_check"]["passed"])

    def test_completed_rerun_reuses_every_stage_without_new_commit(self) -> None:
        engine = self.workspace.engine("runtime_rerun")
        first = engine.run()
        second = engine.run()
        self.assertEqual(first["result_digest"], second["result_digest"])
        self.assertEqual(second["final_version"], first["final_version"])
        self.assertEqual(
            [item["action"] for item in second["actions"]],
            ["reuse", "reuse", "reuse", "reuse"],
        )

    def test_kill_during_revision_update_keeps_old_outputs_from_wrong_use(self) -> None:
        combo_runtime = self.workspace.root / "runtime_combo"
        reference_runtime = self.workspace.root / "runtime_combo_reference"
        old_engine = self.workspace.engine("runtime_combo")
        old_engine.run()
        old_snapshot = old_engine.latest_snapshot()
        reference_engine = self.workspace.engine("runtime_combo_reference")
        reference_engine.run()

        view = load_manifest(self.workspace.manifest)
        layers = list(view.layers)
        layers[1] = replace(
            layers[1], weight_revision=layers[1].weight_revision + 1
        )
        updated_manifest = self.workspace.root / "manifest_combo_v2.jsonl"
        write_manifest(
            updated_manifest, ManifestView.from_layers(view.model_tag, layers)
        )

        killed = _kill_worker(
            updated_manifest,
            self.workspace.calibration,
            combo_runtime,
            "layer_quantization",
            1,
        )
        self.assertEqual(killed.returncode, 137)
        resumed = self.workspace.engine(
            "runtime_combo", manifest=updated_manifest
        )
        resumed_report = resumed.run()
        resumed_snapshot = resumed.latest_snapshot()
        self.assertEqual(
            resumed_report["recovery"]["latest_committed"]["stage"],
            "model_package_validation",
        )
        self.assertEqual(
            resumed_report["recovery"]["discarded_uncommitted"]["layer_id"],
            "layer_1",
        )
        quant_stage = resumed_snapshot["stages"]["layer_quantization"]
        self.assertEqual(quant_stage["reused_layers"], ["layer_0"])
        self.assertEqual(
            quant_stage["rebuilt_layers"], ["layer_1", "layer_2", "layer_3"]
        )
        self.assertEqual(
            quant_stage["layers"]["layer_0"]["checksum"],
            old_snapshot["stages"]["layer_quantization"]["layers"]["layer_0"][
                "checksum"
            ],
        )

        reference_updated = self.workspace.engine(
            "runtime_combo_reference", manifest=updated_manifest
        )
        reference_report = reference_updated.run()
        self.assertEqual(
            resumed_report["result_digest"], reference_report["result_digest"]
        )


if __name__ == "__main__":
    unittest.main()
