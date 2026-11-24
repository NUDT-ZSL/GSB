from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from soundfield import errors as E
from soundfield.images import enumerate_images
from soundfield.pipeline import run_pipeline
from soundfield.scene import load_scene

from .conftest import make_workspace


class EdgeCaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = make_workspace(self.tmp)

    def test_zero_order_is_direct_sound_only(self) -> None:
        scene = json.loads(self.ws["scene"].read_text("utf-8"))
        scene["reconstruction_order"] = 0
        scene["max_reflections"] = 0
        self.ws["scene"].write_text(json.dumps(scene), encoding="utf-8")
        loaded = load_scene(str(self.ws["scene"]))
        images = enumerate_images(loaded)
        self.assertEqual(len(images), len(loaded.sources))
        self.assertTrue(all(img.order == 0 for img in images))
        artifacts = run_pipeline(
            str(self.ws["scene"]), self.ws["state"], self.ws["out"], clean=True
        )
        self.assertEqual(artifacts.report["business_code"], E.E_OK)

    def test_budget_violation_raises_business_code(self) -> None:
        scene = json.loads(self.ws["scene"].read_text("utf-8"))
        scene["memory_budget_bytes"] = 1024
        self.ws["scene"].write_text(json.dumps(scene), encoding="utf-8")
        with self.assertRaises(E.BusinessError) as ctx:
            run_pipeline(
                str(self.ws["scene"]),
                self.tmp / "state_budget",
                self.tmp / "out_budget",
                clean=True,
            )
        self.assertEqual(ctx.exception.code, E.E_BUDGET)

    def test_wav_rate_mismatch(self) -> None:
        scene = json.loads(self.ws["scene"].read_text("utf-8"))
        scene["sample_rate"] = 96000
        self.ws["scene"].write_text(json.dumps(scene), encoding="utf-8")
        with self.assertRaises(E.BusinessError) as ctx:
            run_pipeline(
                str(self.ws["scene"]),
                self.tmp / "state_rate",
                self.tmp / "out_rate",
                clean=True,
            )
        self.assertEqual(ctx.exception.code, E.E_WAV)


if __name__ == "__main__":
    unittest.main()
