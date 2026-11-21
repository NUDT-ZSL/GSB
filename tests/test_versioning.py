from __future__ import annotations

import unittest
from dataclasses import replace

from quant_diag.demo import _rollback_guard
from quant_diag.manifest import ManifestView, load_manifest, write_manifest
from quant_diag.sample_data import load_calibration, write_calibration

from tests.helpers import Workspace


class VersioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Workspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_higher_revision_rebuilds_only_downstream_suffix(self) -> None:
        base_engine = self.workspace.engine("runtime_revision")
        base_report = base_engine.run()
        base_snapshot = base_engine.latest_snapshot()
        view = load_manifest(self.workspace.manifest)
        layers = list(view.layers)
        layers[1] = replace(layers[1], weight_revision=layers[1].weight_revision + 1)
        updated_manifest = self.workspace.root / "manifest_v2.jsonl"
        write_manifest(updated_manifest, ManifestView.from_layers(view.model_tag, layers))
        updated_engine = self.workspace.engine(
            "runtime_revision", manifest=updated_manifest
        )
        updated_report = updated_engine.run()
        updated_snapshot = updated_engine.latest_snapshot()
        affected = ["layer_1", "layer_2", "layer_3"]
        for stage in (
            "layer_quantization",
            "calibration_statistics",
            "layer_inference",
        ):
            stage_data = updated_snapshot["stages"][stage]
            self.assertEqual(stage_data["affected_layers"], affected)
            self.assertEqual(stage_data["reused_layers"], ["layer_0"])
            self.assertEqual(stage_data["rebuilt_layers"], affected)
        self.assertEqual(
            base_snapshot["stages"]["layer_quantization"]["layers"]["layer_0"][
                "checksum"
            ],
            updated_snapshot["stages"]["layer_quantization"]["layers"]["layer_0"][
                "checksum"
            ],
        )
        self.assertNotEqual(base_report["result_digest"], updated_report["result_digest"])
        self.assertTrue(updated_report["self_check"]["passed"])
        self.assertEqual(updated_report["stage_versions"]["model_package_validation"], 5)
        self.assertEqual(updated_report["stage_versions"]["layer_inference"], 8)
        rollback = _rollback_guard(self.workspace.root / "runtime_revision")
        self.assertTrue(rollback["raised"])
        self.assertEqual(rollback["error"]["code"], "E_SNAPSHOT_ROLLBACK")

    def test_changed_calibration_invalidates_layer_and_downstream(self) -> None:
        engine = self.workspace.engine("runtime_calibration")
        engine.run()
        baseline = engine.latest_snapshot()
        samples = load_calibration(self.workspace.calibration, engine.manifest)
        changed = []
        for sample in samples:
            if sample.layer_id == "layer_1" and sample.sample_id.endswith("000"):
                vector = list(sample.vector)
                vector[0] += 0.125
                changed.append(replace(sample, vector=tuple(vector)))
            else:
                changed.append(sample)
        calibration_v2 = self.workspace.root / "calibration_v2.jsonl"
        write_calibration(calibration_v2, changed)
        updated = self.workspace.engine(
            "runtime_calibration", calibration=calibration_v2
        )
        report = updated.run()
        snapshot = updated.latest_snapshot()
        stage3 = snapshot["stages"]["calibration_statistics"]
        stage4 = snapshot["stages"]["layer_inference"]
        self.assertEqual(stage3["affected_layers"], ["layer_1", "layer_2", "layer_3"])
        self.assertEqual(stage3["reused_layers"], ["layer_0"])
        self.assertEqual(stage4["affected_layers"], ["layer_1", "layer_2", "layer_3"])
        self.assertEqual(
            baseline["stages"]["calibration_statistics"]["layers"]["layer_0"][
                "checksum"
            ],
            stage3["layers"]["layer_0"]["checksum"],
        )
        self.assertNotEqual(
            baseline["stages"]["calibration_statistics"]["layers"]["layer_1"][
                "checksum"
            ],
            stage3["layers"]["layer_1"]["checksum"],
        )
        self.assertTrue(report["self_check"]["passed"])


if __name__ == "__main__":
    unittest.main()
