"""流水线确定性与分块/全局逐字节一致性测试."""

from __future__ import annotations

import unittest

import numpy as np

from genapp.blocks import block_ranges, parse_grid
from genapp.pipeline import (
    aggregate,
    derive_fusion_params,
    initial_field,
    iterate_block,
    iterate_global,
    run_global,
    seed_field,
)
from genapp.textcond import condition_vector

from .helpers import TOKENS, small_cfg


class TestDeterminism(unittest.TestCase):
    def test_full_pipeline_byte_identical_across_runs(self):
        cfg = small_cfg()
        out1 = run_global(cfg, condition_vector(TOKENS, cfg.cond_dim, cfg.dtype))
        out2 = run_global(cfg, condition_vector(TOKENS, cfg.cond_dim, cfg.dtype))
        self.assertEqual(out1["image"], out2["image"])
        self.assertTrue(np.array_equal(out1["field0"], out2["field0"]))
        for a, b in zip(out1["intermediates"], out2["intermediates"]):
            self.assertTrue(np.array_equal(a, b))

    def test_condition_vector_order_independent(self):
        cfg = small_cfg()
        v1 = condition_vector(TOKENS, cfg.cond_dim, cfg.dtype)
        v2 = condition_vector(list(reversed(TOKENS)), cfg.cond_dim, cfg.dtype)
        self.assertEqual(v1.tobytes(), v2.tobytes())

    def test_precision_float64_deterministic(self):
        cfg = small_cfg(precision="float64")
        out1 = run_global(cfg, condition_vector(TOKENS, cfg.cond_dim, cfg.dtype))
        out2 = run_global(cfg, condition_vector(TOKENS, cfg.cond_dim, cfg.dtype))
        self.assertEqual(out1["image"], out2["image"])


class TestBlockGlobalConsistency(unittest.TestCase):
    def test_block_fields_match_global_for_all_grids(self):
        cfg = small_cfg()
        cond = condition_vector(TOKENS, cfg.cond_dim, cfg.dtype)
        fusion = derive_fusion_params(cond, cfg)
        noise = seed_field(cfg)
        field0 = initial_field(cfg, fusion, noise=noise)
        global_final, _ = iterate_global(cfg, fusion, field0)
        for grid in cfg.grid_candidates:
            rows, cols = parse_grid(grid)
            assembled = np.empty_like(global_final)
            for region in block_ranges(cfg, rows, cols):
                block = iterate_block(cfg, fusion, region, noise=noise)
                y0, y1, x0, x1 = region
                assembled[y0:y1, x0:x1, :] = block
            self.assertTrue(
                np.array_equal(assembled, global_final),
                f"field mismatch for grid {grid}",
            )
            self.assertEqual(
                aggregate(assembled),
                aggregate(global_final),
                f"image mismatch for grid {grid}",
            )

    def test_block_consistency_with_anchor(self):
        cfg = small_cfg()
        cond = condition_vector(TOKENS, cfg.cond_dim, cfg.dtype)
        fusion = derive_fusion_params(cond, cfg)
        noise = seed_field(cfg)
        rng = np.random.default_rng(99)
        anchor = rng.random(
            (cfg.height, cfg.width, cfg.channels), dtype=np.float64
        ).astype(cfg.dtype)
        field0 = initial_field(cfg, fusion, noise=noise)
        global_final, _ = iterate_global(
            cfg, fusion, field0, anchor=anchor, anchor_strength=0.4
        )
        rows, cols = parse_grid("4x4")
        assembled = np.empty_like(global_final)
        for region in block_ranges(cfg, rows, cols):
            block = iterate_block(
                cfg, fusion, region, noise=noise,
                anchor=anchor, anchor_strength=0.4,
            )
            y0, y1, x0, x1 = region
            assembled[y0:y1, x0:x1, :] = block
        self.assertTrue(np.array_equal(assembled, global_final))


if __name__ == "__main__":
    unittest.main()
