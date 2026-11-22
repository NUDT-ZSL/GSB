from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from quantdiag.errors import DiagnosticError, E_SEMANTIC_DRIFT
from quantdiag.fixtures import generate_fixtures
from quantdiag.formats import upgrade_v1_to_v2, upgrade_v2_to_v3
from quantdiag.inference import (
    DeterministicEngine,
    adjudicate_semantic_equivalence,
)
from quantdiag.service import Workbench, load_fixtures
from quantdiag.snapshots import build_snapshot


class SemanticsAndServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tempdir.name) / "data"
        generate_fixtures(cls.root, force=True)
        cls.bundle = load_fixtures(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tempdir.cleanup()

    def _equivalent_snapshots(self):
        bundle = self.bundle
        return [
            build_snapshot(
                bundle.v1,
                bundle.calibration,
                version=101,
                model_fingerprint=bundle.weights.fingerprint(),
            ),
            build_snapshot(
                bundle.v2,
                bundle.calibration,
                version=102,
                model_fingerprint=bundle.weights.fingerprint(),
            ),
            build_snapshot(
                bundle.v3,
                bundle.calibration,
                version=103,
                model_fingerprint=bundle.weights.fingerprint(),
            ),
        ]

    def test_cross_format_equivalence_and_error_spectrum(self) -> None:
        engine = DeterministicEngine(self.bundle.weights)
        snapshots = self._equivalent_snapshots()
        result = adjudicate_semantic_equivalence(
            engine, snapshots, self.bundle.images
        )
        self.assertTrue(result["equivalent"])
        self.assertEqual(len(set(result["scores_checksums"])), 1)
        self.assertEqual(result["score_errors"][0], result["score_errors"][1])
        self.assertEqual(result["score_errors"][1], result["score_errors"][2])
        spectrum = result["spectra"][1]
        self.assertTrue(spectrum["layers"])
        for layer in spectrum["layers"]:
            self.assertEqual(
                set(layer["weight_rounding"]),
                {"p50", "p90", "p99", "max"},
            )
            self.assertIn("amplification_ratio", layer)
            self.assertIn(
                layer["dominant_source_level"],
                {"weight_rounding", "per_channel_scaling", "calibration_range"},
            )

    def test_semantic_drift_reports_first_difference_layer(self) -> None:
        engine = DeterministicEngine(self.bundle.weights)
        baseline = self._equivalent_snapshots()[1]
        drift = build_snapshot(
            self.bundle.v2_drift,
            self.bundle.calibration,
            version=104,
            model_fingerprint=self.bundle.weights.fingerprint(),
        )
        with self.assertRaises(DiagnosticError) as context:
            adjudicate_semantic_equivalence(
                engine, [baseline, drift], self.bundle.images
            )
        self.assertEqual(context.exception.code, E_SEMANTIC_DRIFT)
        self.assertEqual(context.exception.layer, "conv2")

    def test_migration_chain_remains_equivalent(self) -> None:
        engine = DeterministicEngine(self.bundle.weights)
        v1 = self._equivalent_snapshots()[0]
        v2 = upgrade_v1_to_v2(self.bundle.v1)
        v3 = upgrade_v2_to_v3(v2.params, self.bundle.calibration)
        migrated = [
            v1,
            build_snapshot(
                v2.params,
                self.bundle.calibration,
                version=105,
                model_fingerprint=self.bundle.weights.fingerprint(),
            ),
            build_snapshot(
                v3.params,
                self.bundle.calibration,
                version=106,
                model_fingerprint=self.bundle.weights.fingerprint(),
            ),
        ]
        result = adjudicate_semantic_equivalence(
            engine, migrated, self.bundle.images
        )
        self.assertTrue(result["equivalent"])
        self.assertEqual(len(set(result["scores_checksums"])), 1)

    def test_fixture_generation_is_deterministic(self) -> None:
        first = generate_fixtures(self.root, force=True)
        first_manifest = json.loads(
            (self.root / "manifest.json").read_text(encoding="utf-8")
        )
        second = generate_fixtures(self.root, force=True)
        second_manifest = json.loads(
            (self.root / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            first["manifest_checksum"], second["manifest_checksum"]
        )
        self.assertEqual(
            first_manifest["files"], second_manifest["files"]
        )

    def test_workbench_acceptance_passes_all_checks(self) -> None:
        report = Workbench(self.root).run_acceptance()
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["checks"]), 7)
        codes = {item["code"] for item in report["recent_errors"]}
        self.assertEqual(
            codes,
            {
                "E_VERSION_UNSUPPORTED",
                "E_FORMAT_CORRUPT",
                "E_STALE_RESULT",
                "E_LINEAGE_BROKEN",
                "E_SEMANTIC_DRIFT",
                "E_SNAPSHOT_ROLLBACK",
                "E_SNAPSHOT_CORRUPT",
                "E_STAGE_BUSY",
            },
        )


class GuiSmokeTests(unittest.TestCase):
    def test_offscreen_gui_constructs_and_populates(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PyQt5.QtWidgets import QApplication

            from quantdiag.gui import DiagnosticWindow
        except ImportError as exc:
            self.skipTest(f"PyQt5 unavailable: {exc}")
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name) / "data"
        generate_fixtures(root)
        app = QApplication.instance() or QApplication([])
        window = DiagnosticWindow(root, autorun=True)
        self.assertEqual(window.tabs.count(), 5)
        self.assertGreater(window.layers_table.rowCount(), 0)
        self.assertGreater(window.spectrum_table.rowCount(), 0)
        self.assertGreater(window.errors_table.rowCount(), 0)
        self.assertIn("PASS", window.status_label.text())
        window.close()
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()
