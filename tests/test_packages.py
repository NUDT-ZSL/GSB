import tempfile
import unittest
from pathlib import Path

import numpy as np

from panseg.errors import E_MASK_LENGTH_MISMATCH, E_PALETTE_MISSING
from panseg.rle import bitmap_to_runs
from panseg.v1_package import V1RawInstance, parse_v1_package, write_v1_package
from panseg.v2_package import V2RawInstance, parse_v2_package, write_v2_package


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        bitmap = np.zeros((4, 5), dtype=np.uint8)
        bitmap[1:3, 0:3] = 1
        self.runs, self.pixels = bitmap_to_runs(bitmap)

    def tearDown(self):
        self.tmp.cleanup()

    def test_v1_roundtrip_both_levels(self):
        for compression in (0, 1):
            path = self.root / f"pkg-{compression}.bin"
            raw = [V1RawInstance("a-0", "car", self.runs, self.pixels)]
            write_v1_package(path, 5, 4, raw, compression)
            width, height, parsed = parse_v1_package(path)
            self.assertEqual((width, height), (5, 4))
            self.assertEqual(parsed[0].runs, self.runs)
            self.assertEqual(parsed[0].semantic_class, "car")

    def test_v2_roundtrip_and_palette(self):
        path = self.root / "pkg.json"
        palette = {"color_car": {"rgb": [1, 2, 3]}}
        raw = [
            V2RawInstance(
                "b-0", "car", self.runs, self.pixels, 0.91, False, "color_car", (1, 2, 3)
            )
        ]
        write_v2_package(path, 5, 4, raw, palette)
        width, height, parsed, parsed_palette = parse_v2_package(path)
        self.assertEqual((width, height), (5, 4))
        self.assertEqual(parsed[0].runs, self.runs)
        self.assertTrue(parsed[0].confidence == 0.91)
        self.assertFalse(parsed[0].is_crowd)
        self.assertEqual(parsed_palette["color_car"]["rgb"], [1, 2, 3])

    def test_v2_length_mismatch(self):
        import base64
        import json

        path = self.root / "bad-length.json"
        document = {
            "format_version": 2,
            "width": 5,
            "height": 4,
            "palette": {"color_car": {"rgb": [1, 2, 3]}},
            "instances": [
                {
                    "instance_id": "x",
                    "semantic_class": "car",
                    "palette_name": "color_car",
                    "confidence": 0.5,
                    "is_crowd": False,
                    "mask_b64": base64.b64encode(bytes(19)).decode("ascii"),
                }
            ],
        }
        path.write_text(json.dumps(document))
        from panseg.errors import PipelineError

        with self.assertRaises(PipelineError) as ctx:
            parse_v2_package(path)
        self.assertEqual(ctx.exception.code, E_MASK_LENGTH_MISMATCH)

    def test_v2_palette_missing(self):
        path = self.root / "bad-palette.json"
        palette = {"color_car": {"rgb": [1, 2, 3]}}
        raw = [
            V2RawInstance(
                "b-0", "car", self.runs, self.pixels, 0.9, False, "color_gone", (0, 0, 0)
            )
        ]
        write_v2_package(path, 5, 4, raw, palette)
        from panseg.errors import PipelineError

        with self.assertRaises(PipelineError) as ctx:
            parse_v2_package(path)
        self.assertEqual(ctx.exception.code, E_PALETTE_MISSING)


if __name__ == "__main__":
    unittest.main()
