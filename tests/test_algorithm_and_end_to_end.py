from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from clock_halftone.api import run_pipeline
from clock_halftone.config import load_config, validate_config
from clock_halftone.detail import build_detail_report
from clock_halftone.density import build_density_report, calibrate_density_offsets, effective_target_densities
from clock_halftone.diffusion import build_diffusion_context, diffuse
from clock_halftone.dither import build_dither_offsets
from clock_halftone.generator import generate_clock_sample
from clock_halftone.recompute import recompute_part
from clock_halftone.spectrum import build_uniformity_report
from clock_halftone.tone import build_tone_report
from clock_halftone.validation import load_and_validate


class AlgorithmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.sample = Path(cls.temp.name) / "sample"
        generate_clock_sample(cls.sample, seed=314159, size=256)
        cls.config = load_config()
        validate_config(cls.config)
        cls.bundle = load_and_validate(cls.sample, cls.config)
        texture = {
            label: spec["texture_class"] for label, spec in cls.bundle.part_specs.items()
        }
        cls.dither, cls.amplitudes = build_dither_offsets(
            cls.bundle.mask,
            seed=int(cls.bundle.sample["seed"]) ^ int(cls.config["dither"]["seed"]),
            texture_amplitudes=cls.config["dither"]["texture_amplitudes"],
            texture_by_label=texture,
            background_label=cls.bundle.background_label,
        )
        cls.context = build_diffusion_context(
            cls.bundle.gray,
            cls.bundle.mask,
            cls.dither,
            cls.config,
            background_label=cls.bundle.background_label,
        )
        cls.baseline = diffuse(cls.context, {})
        targets = effective_target_densities(cls.bundle)
        cls.corrections, cls.offsets, cls.calibration = calibrate_density_offsets(
            cls.bundle, cls.baseline, targets
        )
        cls.final = diffuse(cls.context, cls.offsets)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_dither_amplitude_tracks_texture_and_is_reproducible(self) -> None:
        again, amplitudes_again = build_dither_offsets(
            self.bundle.mask,
            seed=int(self.bundle.sample["seed"]) ^ int(self.config["dither"]["seed"]),
            texture_amplitudes=self.config["dither"]["texture_amplitudes"],
            texture_by_label={
                label: spec["texture_class"] for label, spec in self.bundle.part_specs.items()
            },
            background_label=self.bundle.background_label,
        )
        self.assertTrue(np.array_equal(self.dither, again))
        self.assertEqual(self.amplitudes, amplitudes_again)
        self.assertLess(
            self.amplitudes[2],
            self.amplitudes[1],
            "spiral dither should be gentler than tooth dither",
        )
        for label, amplitude in self.amplitudes.items():
            values = self.dither[self.bundle.mask == label]
            self.assertLessEqual(float(np.max(np.abs(values))), amplitude + 1e-9)
            self.assertGreater(float(np.max(np.abs(values))), 0.85 * amplitude)

    def test_adaptive_kernel_selection_and_reproducible_quantization(self) -> None:
        self.assertGreater(int(np.count_nonzero(self.context.kernel_class > 0)), 0)
        self.assertGreater(int(np.count_nonzero(self.context.kernel_class == 0)), 0)
        repeat = diffuse(self.context, self.offsets)
        self.assertTrue(np.array_equal(self.final.binary, repeat.binary))
        self.assertTrue(np.array_equal(self.final.incoming_error, repeat.incoming_error))

    def test_residual_conservation_and_density_calibration(self) -> None:
        stats = self.final.stats
        self.assertTrue(stats["residual_conserved"])
        self.assertLessEqual(abs(stats["residual_balance"]), stats["residual_conservation_tolerance"])
        self.assertGreater(stats["weight_clipped_mass"], 0.0)
        density_report = build_density_report(
            self.bundle, self.baseline, self.final, self.calibration
        )
        self.assertTrue(density_report["ok"])
        for part in density_report["parts"]:
            self.assertAlmostEqual(
                part["corrected_density"],
                part["target_density"],
                delta=0.01,
                msg=part["name"],
            )
            self.assertGreater(part["corrected_density"], 0.02)
            self.assertLess(part["corrected_density"], 0.98)

    def test_tone_spectrum_and_detail_reports(self) -> None:
        density_report = build_density_report(
            self.bundle, self.baseline, self.final, self.calibration
        )
        self.assertTrue(density_report["ok"])
        tone = build_tone_report(self.bundle, self.baseline, self.final)
        self.assertTrue(tone["ok"])
        uniformity = build_uniformity_report(self.bundle, self.final)
        self.assertTrue(uniformity["ok"])
        tooth_checks = [
            check
            for part in uniformity["parts"]
            for check in part["angular_tooth_checks"]
            if not check.get("skipped")
        ]
        self.assertTrue(tooth_checks)
        for check in tooth_checks:
            self.assertEqual(check["detected_harmonic"], check["expected_tooth_count"])
        details = build_detail_report(self.bundle, self.final)
        self.assertTrue(details["ok"])
        for region in details["regions"]:
            if region["kind"] == "tooth":
                self.assertEqual(region["anomalies"]["root_break_count"], 0)
                self.assertEqual(region["anomalies"]["tooth_tip_adhesion_count"], 0)
            if region["kind"] == "bore":
                self.assertFalse(region["anomalies"]["hole_closed"])


class EndToEndTests(unittest.TestCase):
    def test_snapshot_and_local_recompute_match_full_recompute(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sample = root / "sample"
            snapshot = root / "snapshot"
            local_dir = root / "local"
            generate_clock_sample(sample, seed=20261128, size=256)
            result = run_pipeline(
                sample,
                config_path="configs/default.json",
                output_dir=snapshot,
                snapshot_id="test_snapshot",
            )
            self.assertTrue(result["ok"])
            self.assertTrue(all(result["report_status"].values()))
            for name in (
                "snapshot.json",
                "state.npz",
                "halftone.npy",
                "incoming_error.npy",
                "quantization_error.npy",
                "reports/tone_error_report.json",
                "reports/uniformity_report.json",
                "reports/detail_fidelity.json",
            ):
                self.assertTrue((snapshot / name).exists(), name)
            local = recompute_part(
                sample,
                "configs/default.json",
                snapshot,
                local_dir,
                part_label=1,
                new_target_density=0.62,
                verify_full=True,
            )
            self.assertTrue(local["ok"])
            self.assertTrue(local["verification"]["pixel_exact"])
            self.assertEqual(local["verification"]["outside_scope_mismatch_pixels"], 0)
            self.assertEqual(local["difference_from_original"]["changed_outside_part"], 0)
            self.assertGreater(local["difference_from_original"]["changed_pixels"], 0)
            local_binary = np.load(local_dir / "halftone.npy")
            full_binary = np.load(local_dir / "verify" / "full_modified_halftone.npy")
            self.assertTrue(np.array_equal(local_binary, full_binary))
            mask = np.load(sample / "mask.npy")
            self.assertTrue(np.array_equal(local_binary[mask != 1], full_binary[mask != 1]))
            diff = json.loads((local_dir / "recompute_diff.json").read_text(encoding="utf-8"))
            outside_scope = (
                int(mask.size)
                - int(local["recompute_scope"]["part_pixels"])
                - int(local["recompute_scope"]["guard_pixels"])
            )
            self.assertEqual(
                diff["state_continuity"]["outside_scope_preserved_pixel_count"],
                outside_scope,
            )


if __name__ == "__main__":
    unittest.main()
