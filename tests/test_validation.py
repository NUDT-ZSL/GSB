from __future__ import annotations

import json
import unittest
from pathlib import Path

from soundfield import errors as E
from soundfield.scene import load_scene

from .conftest import make_workspace, mutate_scene


class ValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.ws = make_workspace(self.tmp)

    def test_valid_scene_loads(self) -> None:
        scene = load_scene(str(self.ws["scene"]))
        self.assertEqual(scene.band_count, 5)
        self.assertEqual(len(scene.sources), 3)
        self.assertEqual(len(scene.speakers), 6)

    def test_bad_json_returns_business_code(self) -> None:
        self.ws["scene"].write_text("{not json", encoding="utf-8")
        with self.assertRaises(E.BusinessError) as ctx:
            load_scene(str(self.ws["scene"]))
        self.assertEqual(ctx.exception.code, E.E_JSON_PARSE)

    def test_absorption_out_of_open_interval(self) -> None:
        scene = json.loads(self.ws["scene"].read_text(encoding="utf-8"))
        scene["absorption"]["x-"][0] = 1.0
        self.ws["scene"].write_text(json.dumps(scene), encoding="utf-8")
        with self.assertRaises(E.BusinessError) as ctx:
            load_scene(str(self.ws["scene"]))
        self.assertEqual(ctx.exception.code, E.E_ABSORPTION)

    def test_zero_dimension(self) -> None:
        scene = json.loads(self.ws["scene"].read_text(encoding="utf-8"))
        scene["room"]["size"][1] = 0.0
        self.ws["scene"].write_text(json.dumps(scene), encoding="utf-8")
        with self.assertRaises(E.BusinessError) as ctx:
            load_scene(str(self.ws["scene"]))
        self.assertEqual(ctx.exception.code, E.E_DIMENSION)

    def test_coordinate_outside_room(self) -> None:
        scene = json.loads(self.ws["scene"].read_text(encoding="utf-8"))
        scene["sources"][0]["position"][0] = 5.0  # 与上边界重合，非严格内部
        self.ws["scene"].write_text(json.dumps(scene), encoding="utf-8")
        with self.assertRaises(E.BusinessError) as ctx:
            load_scene(str(self.ws["scene"]))
        self.assertEqual(ctx.exception.code, E.E_COORDINATE)

    def test_duplicate_points(self) -> None:
        scene = json.loads(self.ws["scene"].read_text(encoding="utf-8"))
        scene["speakers"][1]["position"] = scene["speakers"][0]["position"]
        self.ws["scene"].write_text(json.dumps(scene), encoding="utf-8")
        with self.assertRaises(E.BusinessError) as ctx:
            load_scene(str(self.ws["scene"]))
        self.assertEqual(ctx.exception.code, E.E_DUPLICATE_POINT)

    def test_bad_sample_rate(self) -> None:
        mutate_scene(self.ws["scene"], "sample_rate", 44100)
        with self.assertRaises(E.BusinessError) as ctx:
            load_scene(str(self.ws["scene"]))
        self.assertEqual(ctx.exception.code, E.E_SAMPLE_RATE)

    def test_bad_layout_count(self) -> None:
        scene = json.loads(self.ws["scene"].read_text(encoding="utf-8"))
        scene["layout"] = "stereo"
        self.ws["scene"].write_text(json.dumps(scene), encoding="utf-8")
        with self.assertRaises(E.BusinessError) as ctx:
            load_scene(str(self.ws["scene"]))
        self.assertEqual(ctx.exception.code, E.E_LAYOUT)

    def test_negative_order(self) -> None:
        mutate_scene(self.ws["scene"], "reconstruction_order", -1)
        with self.assertRaises(E.BusinessError) as ctx:
            load_scene(str(self.ws["scene"]))
        self.assertEqual(ctx.exception.code, E.E_ORDER)

    def test_band_edges_not_increasing(self) -> None:
        scene = json.loads(self.ws["scene"].read_text(encoding="utf-8"))
        scene["band_edges_hz"] = [300.0, 300.0, 6000.0]
        self.ws["scene"].write_text(json.dumps(scene), encoding="utf-8")
        with self.assertRaises(E.BusinessError) as ctx:
            load_scene(str(self.ws["scene"]))
        self.assertEqual(ctx.exception.code, E.E_BAND)

    def test_band_edge_beyond_nyquist(self) -> None:
        scene = json.loads(self.ws["scene"].read_text(encoding="utf-8"))
        scene["band_edges_hz"] = [300.0, 24000.0]
        self.ws["scene"].write_text(json.dumps(scene), encoding="utf-8")
        with self.assertRaises(E.BusinessError) as ctx:
            load_scene(str(self.ws["scene"]))
        self.assertEqual(ctx.exception.code, E.E_BAND)

    def test_validation_failure_produces_no_output(self) -> None:
        mutate_scene(self.ws["scene"], "sample_rate", 22050)
        with self.assertRaises(E.BusinessError):
            load_scene(str(self.ws["scene"]))
        if self.ws["out"].exists():
            self.assertEqual(list(self.ws["out"].glob("*")), [])


if __name__ == "__main__":
    unittest.main()
