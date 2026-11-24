from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path

from volumetric_engine.pipeline import run_render
from volumetric_engine.util import read_json
from volumetric_engine.validation import load_scene

from .helpers import write_scene


class ResumeAndAtomicTests(unittest.TestCase):
    def _scene(self, root: Path, **overrides):
        return load_scene(write_scene(root / "case", width=8, height=6, **overrides))

    def test_resume_reuses_blocks_and_skips_same_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = self._scene(Path(temp))
            first = run_render(scene, run_stability=False, collect_trace=True)
            self.assertTrue(first.ok, first.payload)
            output = scene.render.output_dir
            mtimes = {
                name: (output / name).stat().st_mtime_ns
                for name in ("grayscale.png", "depth.npy", "error_report.json", "manifest.json")
            }
            second = run_render(scene, run_stability=False, collect_trace=True)
            self.assertTrue(second.ok, second.payload)
            self.assertEqual(second.payload["state"]["reused_tiles"], 4)
            self.assertEqual(second.payload["state"]["rendered_tiles"], 0)
            self.assertFalse(second.payload["output"]["written"])
            for name, mtime in mtimes.items():
                self.assertEqual((output / name).stat().st_mtime_ns, mtime)

    def test_missing_block_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = self._scene(Path(temp))
            first = run_render(scene, run_stability=False, collect_trace=True)
            self.assertTrue(first.ok, first.payload)
            block_dir = scene.render.state_dir / "blocks" / first.payload["version_id"]
            victim = sorted(block_dir.glob("block_*.json"))[2]
            victim.unlink()
            second = run_render(scene, run_stability=False, collect_trace=True)
            self.assertTrue(second.ok, second.payload)
            self.assertEqual(second.payload["state"]["rendered_tiles"], 1)
            self.assertEqual(second.payload["state"]["reused_tiles"], 3)
            self.assertIn("E_DIGEST", second.payload["warning_codes"])
            self.assertTrue(victim.exists())

    def test_corrupt_digest_and_pixel_count_are_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = self._scene(Path(temp))
            first = run_render(scene, run_stability=False, collect_trace=True)
            self.assertTrue(first.ok, first.payload)
            block_dir = scene.render.state_dir / "blocks" / first.payload["version_id"]
            victim = sorted(block_dir.glob("block_*.json"))[1]
            payload = read_json(victim, "E_DIGEST")
            payload["pixel_count_declared"] += 1
            victim.write_text(json.dumps(payload), encoding="utf-8")
            second = run_render(scene, run_stability=False, collect_trace=True)
            self.assertTrue(second.ok, second.payload)
            self.assertIn("E_DIGEST", second.payload["warning_codes"])
            repaired = read_json(victim, "E_DIGEST")
            self.assertEqual(repaired["pixel_count_declared"], repaired["tile"]["width"] * repaired["tile"]["height"])

    def test_version_change_keeps_old_blocks_and_redoes_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = self._scene(Path(temp))
            first = run_render(scene, run_stability=False, collect_trace=True)
            self.assertTrue(first.ok, first.payload)
            old_version = first.payload["version_id"]
            old_dir = scene.render.state_dir / "blocks" / old_version
            changed = dataclasses.replace(
                scene,
                render=dataclasses.replace(scene.render, step_size=scene.render.step_size * 1.25),
            )
            second = run_render(changed, run_stability=False, collect_trace=True)
            self.assertTrue(second.ok, second.payload)
            self.assertNotEqual(second.payload["version_id"], old_version)
            self.assertEqual(second.payload["state"]["rendered_tiles"], 4)
            self.assertEqual(second.payload["state"]["reused_tiles"], 0)
            self.assertIn("E_VERSION", second.payload["warning_codes"])
            self.assertTrue(old_dir.exists())
            self.assertTrue((scene.render.state_dir / "blocks" / second.payload["version_id"]).exists())

    def test_damaged_same_version_output_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = self._scene(Path(temp))
            first = run_render(scene, run_stability=False, collect_trace=True)
            self.assertTrue(first.ok, first.payload)
            report_path = scene.render.output_dir / "error_report.json"
            report_path.write_text("{}", encoding="utf-8")
            second = run_render(scene, run_stability=False, collect_trace=True)
            self.assertTrue(second.ok, second.payload)
            self.assertTrue(second.payload["output"]["written"])
            manifest = json.loads((scene.render.output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["version_id"], second.payload["output_version_id"])
            self.assertGreater(report_path.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()

