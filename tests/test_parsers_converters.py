import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ieditor.config import load_quant_rules, load_version_map
from ieditor.converter import (downgrade_v2_to_v1, roundtrip_mask_equal,
                               upgrade_v1_to_v2)
from ieditor.errors import (E_FORMAT_INVALID, E_VERSION_UNSUPPORTED,
                            FormatInvalid, VersionUnsupported)
from ieditor.images import load_rgb
from ieditor.models import MASK_DAMAGED, StyleParams, UnifiedProject
from ieditor.parsers import (MAGIC_V1, detect_version, parse_project,
                             parse_v1, parse_v2, write_v1, write_v2)

CONFIG = ROOT / "config"
SAMPLES = ROOT / "samples"


class ParserTests(unittest.TestCase):
    def test_v1_and_v2_parse_same_mask(self):
        for name in ("sample_a", "sample_b", "sample_c"):
            v1 = parse_v1(SAMPLES / f"{name}_v1.ied")
            v2 = parse_v2(SAMPLES / f"{name}_v2.json")
            self.assertTrue(np.array_equal(v1.mask, v2.mask), name)
            self.assertEqual(v1.source_version, 1)
            self.assertEqual(v2.source_version, 2)
            self.assertEqual(detect_version(SAMPLES / f"{name}_v1.ied"), 1)
            self.assertEqual(detect_version(SAMPLES / f"{name}_v2.json"), 2)

    def test_v1_write_roundtrip_pixel_identical(self):
        project = parse_v1(SAMPLES / "sample_b_v1.ied")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rt.ied"
            write_v1(project, out)
            reparsed = parse_v1(out)
            self.assertTrue(roundtrip_mask_equal(project, reparsed))
            self.assertEqual(project.style.intensity,
                             reparsed.style.intensity)

    def test_v2_write_roundtrip_pixel_identical(self):
        project = parse_v2(SAMPLES / "sample_a_v2.json")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rt.json"
            write_v2(project, out)
            reparsed = parse_v2(out)
            self.assertTrue(roundtrip_mask_equal(project, reparsed))
            self.assertEqual(project.style.channel_weights,
                             reparsed.style.channel_weights)

    def test_corrupt_rle_raises_format_invalid(self):
        with self.assertRaises(FormatInvalid) as ctx:
            parse_project(SAMPLES / "corrupt" / "bad_rle_v1.ied")
        self.assertEqual(ctx.exception.code, E_FORMAT_INVALID)

    def test_corrupt_base64_length_raises_format_invalid(self):
        with self.assertRaises(FormatInvalid) as ctx:
            parse_project(SAMPLES / "corrupt" / "bad_base64_v2.json")
        self.assertEqual(ctx.exception.code, E_FORMAT_INVALID)

    def test_unknown_version_raises_version_unsupported(self):
        with self.assertRaises(VersionUnsupported) as ctx:
            parse_project(SAMPLES / "corrupt" / "bad_version_v2.json")
        self.assertEqual(ctx.exception.code, E_VERSION_UNSUPPORTED)

    def test_missing_channel_weight_raises_format_invalid(self):
        with self.assertRaises(FormatInvalid) as ctx:
            parse_project(SAMPLES / "corrupt" / "missing_weight_v2.json")
        self.assertEqual(ctx.exception.code, E_FORMAT_INVALID)
        self.assertIn("B", str(ctx.exception))

    def test_illegal_run_value_detected(self):
        project = parse_v1(SAMPLES / "sample_a_v1.ied")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bad.ied"
            write_v1(project, out)
            blob = bytearray(out.read_bytes())
            # locate first run payload byte and corrupt its value to 9
            run_offset = len(blob) - 2
            blob[run_offset] = 9
            out.write_bytes(bytes(blob))
            with self.assertRaises(FormatInvalid) as ctx:
                parse_v1(out)
            self.assertEqual(ctx.exception.code, E_FORMAT_INVALID)


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.version_map = load_version_map(CONFIG / "version_map.json")
        self.rules = load_quant_rules(CONFIG / "quant_rules.json")

    def test_upgrade_expands_weights_and_palette(self):
        v1 = parse_v1(SAMPLES / "sample_a_v1.ied")
        ref = load_rgb(SAMPLES / v1.style_ref_path)
        v2 = upgrade_v1_to_v2(v1, self.version_map, ref)
        self.assertEqual(v2.source_version, 2)
        expected = self.version_map.weights_for(v1.style.intensity)
        self.assertEqual(v2.style.channel_weights, expected)
        self.assertEqual(len(v2.style.palette),
                         self.version_map.palette_buckets)
        self.assertTrue(roundtrip_mask_equal(v1, v2))

    def test_full_roundtrip_mask_identical_and_loss_log(self):
        v1 = parse_v1(SAMPLES / "sample_a_v1.ied")
        ref = load_rgb(SAMPLES / v1.style_ref_path)
        v2 = upgrade_v1_to_v2(v1, self.version_map, ref)
        back = downgrade_v2_to_v1(v2, self.version_map, self.rules)
        kinds = {entry.kind for entry in back.loss_log}
        self.assertEqual(
            kinds,
            {"intensity_fold", "float_precision",
             "palette_dropped", "mask_bit_width"})
        self.assertTrue(roundtrip_mask_equal(v1, back))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "back.ied"
            write_v1(back, out)
            reparsed = parse_v1(out)
            self.assertTrue(np.array_equal(v1.mask, reparsed.mask))

    def test_unknown_intensity_rejected(self):
        project = UnifiedProject(
            project_id="x", source_version=1, image_path="a.png",
            style_ref_path="b.png",
            mask=np.zeros((4, 4), dtype=np.uint8),
            style=StyleParams(intensity=99, channel_weights={}, palette=[]))
        with self.assertRaises(FormatInvalid):
            self.version_map.weights_for(99)


if __name__ == "__main__":
    unittest.main(verbosity=2)
