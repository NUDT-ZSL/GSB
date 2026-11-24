from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from volumetric_engine.errors import EngineError
from volumetric_engine.validation import load_scene

from .helpers import run_cli, write_scene


class ValidationTests(unittest.TestCase):
    def test_valid_scene(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scene = load_scene(write_scene(root / "case"))
            self.assertEqual(scene.name, "unit-test-scene")
            self.assertEqual(len(scene.media), 1)
            self.assertEqual(scene.render.fixed_scale, 256)

    def test_business_codes(self) -> None:
        cases = [
            ("extinction", lambda p: p["media"][0].__setitem__("extinction", [0.0, 1.0, 1.0]), "E_EXTINCTION"),
            ("albedo", lambda p: p["media"][0].__setitem__("albedo", [1.1, 0.5, 0.5]), "E_ALBEDO"),
            ("ior", lambda p: p["media"][0].__setitem__("ior", 0.9), "E_IOR"),
            ("light_position", lambda p: p["lights"][0].__setitem__("position", [9.0, 0.0, 0.0]), "E_LIGHT_POS"),
            ("light_intensity", lambda p: p["lights"][0].__setitem__("intensity", 0.0), "E_LIGHT_INTENSITY"),
            ("fov", lambda p: p["camera"].__setitem__("vertical_fov_deg", 180.0), "E_FOV"),
            ("aspect", lambda p: p["camera"].__setitem__("aspect_ratio", 0.0), "E_ASPECT"),
            ("fixed_scale", lambda p: p["render"].__setitem__("fixed_scale", 0), "E_FIXED_SCALE"),
            ("tile", lambda p: p["render"].__setitem__("tile", {"width": 0, "height": 3}), "E_TILE"),
            (
                "memory",
                lambda p: p["render"].__setitem__("inflight_memory_limit_bytes", 1),
                "E_MEMORY_BUDGET",
            ),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, mutate, expected in cases:
                with self.subTest(name=name):
                    scene_path = write_scene(root / name, mutate=mutate)
                    with self.assertRaises(EngineError) as raised:
                        load_scene(scene_path)
                    self.assertEqual(raised.exception.code, expected)

    def test_invalid_cli_writes_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scene_path = write_scene(
                root / "case",
                mutate=lambda p: p["media"][0].__setitem__("size", [1.0, -1.0, 1.0]),
            )
            result = run_cli("render", "--scene", str(scene_path), "--no-stability")
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["code"], "E_MEDIA_SIZE")
            self.assertFalse((root / "case" / "output").exists())
            self.assertFalse((root / "case" / "state").exists())

    def test_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scene.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(EngineError) as raised:
                load_scene(path)
            self.assertEqual(raised.exception.code, "E_JSON")


if __name__ == "__main__":
    unittest.main()

