from __future__ import annotations

import tempfile
import unittest
from unittest import mock

import numpy as np

from relief_tiles.errors import BusinessError
from relief_tiles.gray import generate_full_gray, render_block_gray
from relief_tiles.noise import synthesize_block_height, synthesize_height
from relief_tiles.renderer import ReliefRenderer
from tests.helpers import config


class RenderingExactnessTests(unittest.TestCase):
    def test_full_height_is_bit_exact_and_block_concat_matches(self) -> None:
        cfg = config(width=72, height=48, rows=3, cols=4)
        first = synthesize_height(cfg)
        second = synthesize_height(cfg)
        self.assertTrue(np.array_equal(first, second))
        assembled = np.empty_like(first)
        for row, col in cfg.iter_block_indices():
            x0, y0, width, height = cfg.block_bounds(row, col)
            block = synthesize_block_height(cfg, row, col)
            assembled[y0 : y0 + height, x0 : x0 + width] = block
        self.assertTrue(np.array_equal(first, assembled))

    def test_every_block_gray_matches_full_crop_for_multiple_layouts(self) -> None:
        for cols, rows, concurrency, budget in (
            (1, 1, 1, 1),
            (2, 2, 2, 1),
            (3, 2, 8, 4),
            (4, 3, 3, 2),
        ):
            with self.subTest(
                cols=cols, rows=rows, concurrency=concurrency, budget=budget
            ):
                cfg = config(
                    width=12 * cols,
                    height=8 * rows,
                    rows=rows,
                    cols=cols,
                    concurrency=concurrency,
                    budget_blocks=budget,
                )
                full = generate_full_gray(cfg)
                stats = _stats(cfg)
                for row, col in cfg.iter_block_indices():
                    x0, y0, width, height = cfg.block_bounds(row, col)
                    block, _height_window = render_block_gray(cfg, row, col, stats)
                    self.assertTrue(
                        np.array_equal(
                            block, full[y0 : y0 + height, x0 : x0 + width]
                        ),
                        (row, col),
                    )

    def test_concurrency_and_memory_budget_do_not_change_pixels(self) -> None:
        reference = None
        for concurrency, budget in ((1, 1), (2, 2), (4, 3)):
            cfg = config(
                width=72,
                height=48,
                rows=2,
                cols=3,
                concurrency=concurrency,
                budget_blocks=budget,
            )
            with tempfile.TemporaryDirectory() as temporary:
                result = ReliefRenderer(cfg, temporary).render_all()
            if reference is None:
                reference = result.mosaic
            self.assertTrue(np.array_equal(reference, result.mosaic))
            self.assertTrue(result.ok)
            self.assertEqual(result.report["max_abs_diff_vs_full_reference"], 0)
            self.assertLessEqual(
                result.report["peak_in_flight_pixels"],
                cfg.memory_budget_pixels,
            )
            self.assertLessEqual(
                result.report["peak_in_flight_pixels"],
                concurrency * cfg.flight_pixels_per_block,
            )

    def test_block_boundaries_use_real_neighbors(self) -> None:
        cfg = config(width=48, height=32, rows=2, cols=3)
        stats = _stats(cfg)
        full = generate_full_gray(cfg)
        for col in range(1, cfg.cols):
            left = (col - 1) * cfg.block_width
            right = col * cfg.block_width
            left_block, _ = render_block_gray(cfg, 0, col - 1, stats)
            right_block, _ = render_block_gray(cfg, 0, col, stats)
            seam = np.abs(
                left_block[:, -1].astype(np.int32)
                - right_block[:, 0].astype(np.int32)
            )
            expected = np.abs(
                full[0 : cfg.block_height, right - 1].astype(np.int32)
                - full[0 : cfg.block_height, right].astype(np.int32)
            )
            self.assertTrue(np.array_equal(seam, expected))

    def test_first_mismatch_reports_global_coordinate_and_block(self) -> None:
        cfg = config(width=48, height=32, rows=2, cols=3)
        with tempfile.TemporaryDirectory() as temporary:
            renderer = ReliefRenderer(cfg, temporary)
            expected = generate_full_gray(cfg)
            shifted = np.roll(expected, 1, axis=1)
            result = renderer.render_all(reference=shifted)
        self.assertFalse(result.ok)
        mismatch = result.first_mismatch
        self.assertIsNotNone(mismatch)
        assert mismatch is not None
        self.assertIn("x", mismatch)
        self.assertIn("y", mismatch)
        self.assertEqual(mismatch["channel"], 0)
        self.assertIn("row", mismatch["block"])
        self.assertIn("col", mismatch["block"])
        self.assertGreater(result.report["max_abs_diff_vs_full_reference"], 0)

    def test_block_exception_cancels_remaining_work_and_returns_first_error(self) -> None:
        cfg = config(width=72, height=48, rows=2, cols=3)
        with tempfile.TemporaryDirectory() as temporary:
            renderer = ReliefRenderer(cfg, temporary)
            with mock.patch(
                "relief_tiles.renderer._compute_block",
                side_effect=RuntimeError("synthetic block failure"),
            ):
                with self.assertRaises(BusinessError) as caught:
                    renderer.render_all()
        self.assertEqual(caught.exception.code, "E_RENDER")
        self.assertIn("synthetic block failure", caught.exception.details["error"])


def _stats(cfg):
    from relief_tiles.gray import compute_normalization_stats

    return compute_normalization_stats(cfg)


if __name__ == "__main__":
    unittest.main()
