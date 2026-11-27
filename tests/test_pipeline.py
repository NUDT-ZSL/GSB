from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from poisson_fusion.errors import FusionError
from poisson_fusion.io_utils import read_png, sha256_file
from poisson_fusion.metrics import quantize_rgb
from poisson_fusion.pipeline import (
    FusionParameters,
    fuse_image_files,
    replace_foreground_files,
)
from poisson_fusion.sample_gen import generate_sample


class PipelineAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory(prefix="poisson-fusion-tests-")
        cls.root = Path(cls._temp.name)
        cls.samples = cls.root / "samples"
        generate_sample(20261128, cls.samples, height=128, width=160, band_radius=3)
        cls.params = FusionParameters(tolerance=1e-8, max_iterations=3000, band_radius=3)
        cls.initial_dir = cls.root / "initial"
        cls.initial = fuse_image_files(
            cls.samples / "base.npy",
            cls.samples / "foreground_rgba.npy",
            cls.samples / "target_mask.npy",
            cls.initial_dir,
            params=cls.params,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def test_generation_is_byte_reproducible(self) -> None:
        second = self.root / "samples_second"
        metadata = generate_sample(20261128, second, height=128, width=160, band_radius=3)
        first_metadata = json.loads((self.samples / "sample_metadata.json").read_text("utf-8"))
        self.assertEqual(metadata["base_sha256"], first_metadata["base_sha256"])
        for filename in ("base.npy", "foreground_rgba.npy", "target_mask.npy", "boundary_band.npy"):
            self.assertEqual(
                sha256_file(self.samples / filename),
                sha256_file(second / filename),
                filename,
            )

    def test_boundary_matches_base_and_images_are_viewable(self) -> None:
        fused = np.load(self.initial_dir / "fused_u8.npy")
        base = np.load(self.samples / "base.npy")
        boundary = np.load(self.initial_dir / "boundary_mask.npy").astype(bool)
        report = json.loads((self.initial_dir / "report.json").read_text("utf-8"))
        self.assertEqual(report["metrics"]["boundary_match"]["max_error"], 0.0)
        self.assertTrue(np.array_equal(fused[boundary], base[boundary]))
        self.assertTrue(np.array_equal(read_png(self.initial_dir / "fused.png"), fused))
        self.assertTrue(report["solver"]["converged"])
        self.assertGreater(report["solver"]["iterations_max"], 0)

    def test_error_map_statistics_match_report(self) -> None:
        report = json.loads((self.initial_dir / "report.json").read_text("utf-8"))
        band = np.load(self.initial_dir / "boundary_band.npy").astype(bool)
        seam = np.load(self.initial_dir / "seam_error_map.npy")
        gradient = np.load(self.initial_dir / "gradient_error_map.npy")
        seam_values = seam[band].astype(np.float64)
        gradient_values = gradient[band].astype(np.float64)
        self.assertEqual(report["metrics"]["seam_error"]["count"], int(seam_values.size))
        self.assertEqual(report["metrics"]["seam_error"]["max"], float(np.max(seam_values)))
        self.assertEqual(report["metrics"]["seam_error"]["mean"], float(np.mean(seam_values)))
        self.assertEqual(
            report["metrics"]["gradient_error"]["p95"],
            float(np.percentile(gradient_values, 95)),
        )

    def test_gradient_fidelity_is_reported_for_interior(self) -> None:
        report = json.loads((self.initial_dir / "report.json").read_text("utf-8"))
        interior = report["metrics"]["interior_gradient_error"]
        self.assertGreater(interior["count"], 0)
        self.assertLess(interior["mean"], 2.0)
        self.assertLess(interior["p95"], 8.0)

    def test_serialized_fields_are_complete(self) -> None:
        manifest = json.loads(
            (self.initial_dir / "fields" / "fields_manifest.json").read_text("utf-8")
        )
        self.assertEqual(manifest["boundary_condition"]["shape"], [128, 160, 3])
        self.assertEqual(manifest["gradient_field"]["gx"]["shape"], [128, 160, 3])
        self.assertEqual(manifest["gradient_field"]["gy"]["shape"], [128, 160, 3])
        self.assertEqual(manifest["foreground_extraction"]["mode"], "alpha")
        self.assertEqual(manifest["foreground_extraction"]["alpha_threshold"], 1.0)

    def test_full_run_reproducible(self) -> None:
        second_dir = self.root / "initial_repeat"
        fuse_image_files(
            self.samples / "base.npy",
            self.samples / "foreground_rgba.npy",
            self.samples / "target_mask.npy",
            second_dir,
            params=self.params,
        )
        first = np.load(self.initial_dir / "fused_u8.npy")
        second = np.load(second_dir / "fused_u8.npy")
        self.assertTrue(np.array_equal(first, second))
        first_report = json.loads((self.initial_dir / "report.json").read_text("utf-8"))
        second_report = json.loads((second_dir / "report.json").read_text("utf-8"))
        self.assertEqual(first_report["run_id"], second_report["run_id"])
        self.assertEqual(first_report["metrics"], second_report["metrics"])

    def test_solid_background_matches_alpha_extraction(self) -> None:
        solid_dir = self.root / "solid"
        solid = fuse_image_files(
            self.samples / "base.npy",
            self.samples / "foreground_solid.npy",
            self.samples / "target_mask.npy",
            solid_dir,
            params=self.params,
        )
        alpha = np.load(self.initial_dir / "fused_u8.npy")
        solid_image = np.load(solid_dir / "fused_u8.npy")
        self.assertTrue(np.array_equal(alpha, solid_image))
        self.assertEqual(solid["ok"], True)

    def test_same_mask_incremental_matches_full_recompute(self) -> None:
        output = self.root / "replace_same"
        result = replace_foreground_files(
            self.initial_dir / "state.json",
            self.samples / "foreground_v2_rgba.npy",
            output,
            params_override=self.params,
        )
        verification = result["incremental_verification"]
        self.assertTrue(verification["within_tolerance"])
        self.assertLessEqual(verification["max_abs_difference"], 1)
        self.assertIsNone(verification["first_over_tolerance_pixel"])
        self.assertTrue(verification["unaffected_pixels_exactly_preserved"])
        self.assertFalse(result["mask_change"]["detected"])
        updated = np.load(output / "updated_region_mask.npy").astype(bool)
        mask = np.load(self.samples / "target_mask.npy").astype(bool)
        self.assertTrue(np.array_equal(updated, mask))

    def test_changed_mask_incremental_and_difference_artifacts(self) -> None:
        output = self.root / "replace_changed"
        result = replace_foreground_files(
            self.initial_dir / "state.json",
            self.samples / "foreground_v2_rgba.npy",
            output,
            new_mask_path=self.samples / "target_mask_v2.npy",
            params_override=self.params,
        )
        verification = result["incremental_verification"]
        self.assertTrue(verification["within_tolerance"])
        self.assertEqual(verification["max_abs_difference"], 0)
        self.assertIsNone(verification["first_over_tolerance_pixel"])
        self.assertTrue(verification["unaffected_pixels_exactly_preserved"])
        mask_change = result["mask_change"]
        self.assertTrue(mask_change["detected"])
        self.assertGreater(mask_change["old_only"]["pixels"], 0)
        self.assertGreater(mask_change["new_only"]["pixels"], 0)
        self.assertGreater(mask_change["xor_pixels"], 0)
        for name in ("mask_old_only", "mask_new_only", "mask_change_xor"):
            self.assertTrue((output / f"{name}.png").exists())
            self.assertTrue((output / f"{name}.npy").exists())
        old_only = np.load(output / "mask_old_only.npy").astype(bool)
        fused = np.load(output / "fused_u8.npy")
        base = np.load(self.samples / "base.npy")
        self.assertTrue(np.array_equal(fused[old_only], base[old_only]))

    def test_distinct_business_errors(self) -> None:
        invalid = self.samples / "invalid"
        cases = [
            ("empty_mask.npy", "E_EMPTY_MASK", 2001),
            ("full_mask.npy", "E_FULL_MASK", 2002),
            ("broken_mask.npy", "E_BROKEN_BOUNDARY", 2003),
        ]
        for filename, code, numeric in cases:
            with self.subTest(filename=filename):
                with self.assertRaises(FusionError) as caught:
                    fuse_image_files(
                        self.samples / "base.npy",
                        self.samples / "foreground_rgba.npy",
                        invalid / filename,
                        self.root / f"error_{filename}",
                        params=self.params,
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.numeric_code, numeric)
                self.assertFalse((self.root / f"error_{filename}" / "fused.png").exists())

        with self.assertRaises(FusionError) as caught:
            fuse_image_files(
                self.samples / "base.npy",
                invalid / "mismatched_foreground.npy",
                self.samples / "target_mask.npy",
                self.root / "error_dimensions",
                params=self.params,
            )
        self.assertEqual(caught.exception.code, "E_DIMENSIONS")
        self.assertEqual(caught.exception.numeric_code, 1101)

    def test_nonconvergence_has_pixel_and_iteration_without_fused_output(self) -> None:
        output = self.root / "nonconverged"
        bad_params = FusionParameters(
            tolerance=1e-20,
            max_iterations=1,
            band_radius=3,
        )
        with self.assertRaises(FusionError) as caught:
            fuse_image_files(
                self.samples / "base.npy",
                self.samples / "foreground_rgba.npy",
                self.samples / "target_mask.npy",
                output,
                params=bad_params,
            )
        error = caught.exception
        self.assertEqual(error.code, "E_SOLVER_NOT_CONVERGED")
        self.assertIn("pixel", error.details)
        self.assertIn("iteration", error.details)
        self.assertFalse((output / "fused.png").exists())
        self.assertTrue((output / "error.json").exists())

    def test_nonfinite_input_reports_first_pixel(self) -> None:
        bad_foreground = self.root / "bad_foreground.npy"
        foreground = np.load(self.samples / "foreground_rgba.npy").astype(np.float64)
        foreground[20, 30, 1] = np.nan
        np.save(bad_foreground, foreground)
        with self.assertRaises(FusionError) as caught:
            fuse_image_files(
                self.samples / "base.npy",
                bad_foreground,
                self.samples / "target_mask.npy",
                self.root / "nonfinite",
                params=self.params,
            )
        self.assertEqual(caught.exception.code, "E_NONFINITE_INPUT")
        self.assertEqual(caught.exception.details["first_pixel"], [20, 30])

    def test_run_log_is_append_only(self) -> None:
        log_path = self.root / "run_log.jsonl"
        output_a = self.root / "log_run_a"
        output_b = self.root / "log_run_b"
        for output in (output_a, output_b):
            fuse_image_files(
                self.samples / "base.npy",
                self.samples / "foreground_rgba.npy",
                self.samples / "target_mask.npy",
                output,
                params=self.params,
                log_path=log_path,
            )
        lines = log_path.read_text("utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            record = json.loads(line)
            self.assertEqual(record["status"], "ok")
            self.assertIn("input_summary", record)
            self.assertIn("key_metrics", record)
            self.assertIn("output_files", record)


if __name__ == "__main__":
    unittest.main()
