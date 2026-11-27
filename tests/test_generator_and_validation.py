from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import numpy as np

from clock_halftone.config import load_config, validate_config
from clock_halftone.errors import BusinessError
from clock_halftone.generator import generate_clock_sample
from clock_halftone.io import read_pgm, write_pgm
from clock_halftone.validation import load_and_validate


class GeneratorTests(unittest.TestCase):
    def test_generator_is_byte_deterministic_and_seed_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            third = Path(temp) / "third"
            generate_clock_sample(first, seed=1234, size=192)
            generate_clock_sample(second, seed=1234, size=192)
            generate_clock_sample(third, seed=1235, size=192)
            for name in ("gray.npy", "mask.npy", "details.json"):
                self.assertEqual(
                    (first / name).read_bytes(),
                    (second / name).read_bytes(),
                    name,
                )
            self.assertNotEqual(
                (first / "gray.npy").read_bytes(),
                (third / "gray.npy").read_bytes(),
            )
            mask = np.load(first / "mask.npy")
            self.assertEqual(sorted(np.unique(mask).tolist()), [0, 1, 2, 3, 4])
            for label in (1, 2, 3, 4):
                self.assertGreater(int(np.count_nonzero(mask == label)), 0)

    def test_pgm_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "image.pgm"
            image = np.arange(35, dtype=np.uint8).reshape(5, 7)
            write_pgm(path, image)
            self.assertTrue(np.array_equal(read_pgm(path), image))


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sample = Path(self.temp.name) / "sample"
        generate_clock_sample(self.sample, seed=99, size=192)
        self.config = load_config()
        validate_config(self.config)
        self.mask = np.load(self.sample / "mask.npy")
        self.gray = np.load(self.sample / "gray.npy")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_code(self, code: str) -> None:
        with self.assertRaises(BusinessError) as captured:
            load_and_validate(self.sample, self.config)
        self.assertEqual(captured.exception.code, code)

    def test_unknown_mask_label(self) -> None:
        broken = self.mask.copy()
        broken[5, 5] = 9
        np.save(self.sample / "mask.npy", broken)
        self.assert_code("E_MASK_UNKNOWN_LABEL")
        np.save(self.sample / "mask.npy", self.mask)

    def test_missing_label_pixels(self) -> None:
        broken = self.mask.copy()
        broken[broken == 4] = 0
        np.save(self.sample / "mask.npy", broken)
        self.assert_code("E_MASK_LABEL_MISSING")
        np.save(self.sample / "mask.npy", self.mask)

    def test_missing_pixel_marker(self) -> None:
        broken = self.mask.copy()
        broken[8, 8] = -1
        np.save(self.sample / "mask.npy", broken)
        self.assert_code("E_MASK_MISSING_PIXELS")
        np.save(self.sample / "mask.npy", self.mask)

    def test_mask_size_mismatch(self) -> None:
        np.save(self.sample / "mask.npy", self.mask[:-1, :-1])
        self.assert_code("E_MASK_SIZE")
        np.save(self.sample / "mask.npy", self.mask)

    def test_gray_depth_mismatch(self) -> None:
        np.save(self.sample / "gray.npy", self.gray.astype(np.uint16) * 2)
        self.assert_code("E_GRAY_DEPTH")
        np.save(self.sample / "gray.npy", self.gray)

    def test_optional_hash_verification(self) -> None:
        changed = self.gray.copy()
        changed[3, 3] = int(changed[3, 3]) ^ 1
        np.save(self.sample / "gray.npy", changed)
        with self.assertRaises(BusinessError) as captured:
            load_and_validate(self.sample, self.config, verify_hashes=True)
        self.assertEqual(captured.exception.code, "E_INPUT_HASH")
        np.save(self.sample / "gray.npy", self.gray)

    def test_print_grid_constraints(self) -> None:
        too_fine = copy.deepcopy(self.config)
        too_fine["print"]["lines_per_inch"] = 500.0
        self.config = too_fine
        self.assert_code("E_PRINT_LPI")
        self.config = copy.deepcopy(self.config)
        self.config = load_config()
        self.config["print"]["ink_dot_diameter_mm"] = 2.0
        self.assert_code("E_PRINT_DOT_SIZE")

    def test_valid_analysis_contains_histograms_and_capacity(self) -> None:
        bundle = load_and_validate(self.sample, self.config)
        from clock_halftone.validation import build_input_analysis

        analysis = build_input_analysis(bundle)
        self.assertTrue(analysis["ok"])
        self.assertEqual(len(analysis["parts"]), 4)
        for part in analysis["parts"]:
            self.assertEqual(len(part["gray_histogram_256"]), 256)
            self.assertGreater(part["print_cells_floor_by_area"], 0)
        self.assertGreater(analysis["print_grid"]["screen_pitch_px"][0], 1.0)


if __name__ == "__main__":
    unittest.main()
