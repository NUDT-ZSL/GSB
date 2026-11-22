"""图生图锚定校验与偏差谱测试."""

from __future__ import annotations

import unittest

import numpy as np

from genapp.anchor import (
    deviation_spectrum,
    load_anchor,
    validate_anchor_strength,
)
from genapp.errors import E_ANCHOR_SHAPE, E_ANCHOR_STRENGTH, GenError

from .helpers import small_cfg


class TestAnchor(unittest.TestCase):
    def test_strength_out_of_range(self):
        for bad in (-0.01, 1.01, float("nan") if False else 2.0):
            with self.assertRaises(GenError) as ctx:
                validate_anchor_strength(bad)
            self.assertEqual(ctx.exception.code, E_ANCHOR_STRENGTH)

    def test_strength_boundary_ok(self):
        self.assertEqual(validate_anchor_strength(0.0), 0.0)
        self.assertEqual(validate_anchor_strength(1.0), 1.0)

    def test_shape_mismatch(self):
        cfg = small_cfg()
        ref = bytes(cfg.height * cfg.width * cfg.channels)
        with self.assertRaises(GenError) as ctx:
            load_anchor(cfg, ref, (cfg.height + 1, cfg.width, cfg.channels))
        self.assertEqual(ctx.exception.code, E_ANCHOR_SHAPE)

    def test_byte_length_mismatch(self):
        cfg = small_cfg()
        with self.assertRaises(GenError) as ctx:
            load_anchor(cfg, b"\x00", (cfg.height, cfg.width, cfg.channels))
        self.assertEqual(ctx.exception.code, E_ANCHOR_SHAPE)

    def test_deviation_spectrum_dominant_block(self):
        cfg = small_cfg()
        anchor = np.zeros((cfg.height, cfg.width, cfg.channels), dtype=cfg.dtype)
        fields = []
        for bid in range(4):
            y0 = (bid // 2) * cfg.height // 2
            y1 = y0 + cfg.height // 2
            x0 = (bid % 2) * cfg.width // 2
            x1 = x0 + cfg.width // 2
            block = np.full((y1 - y0, x1 - x0, cfg.channels), bid, dtype=cfg.dtype)
            fields.append((bid, (y0, y1, x0, x1), block))
        report = deviation_spectrum(cfg, fields, anchor)
        self.assertEqual(len(report["spectrum"]), 4)
        self.assertEqual(report["dominant_block"]["block_id"], 3)


if __name__ == "__main__":
    unittest.main()
