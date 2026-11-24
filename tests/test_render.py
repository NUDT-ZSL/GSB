from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from volumetric_engine.pipeline import run_render
from volumetric_engine.memory import tile_buffer_bytes as budget_for
from volumetric_engine.renderer import TRACE_COLUMNS, render_pixel, tile_geometries
from volumetric_engine.scheduler import render_blocks_in_memory, tile_buffer_bytes
from volumetric_engine.validation import load_scene

from .helpers import write_scene


class RenderTests(unittest.TestCase):
    def test_fixed_path_records_first_exceed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = load_scene(
                write_scene(
                    Path(temp) / "case",
                    fixed_scale=64,
                    step_size=0.25,
                    max_steps=128,
                    transmittance_cutoff=0.2,
                    transmittance_tolerance=1e-5,
                )
            )
            pixel, rows = render_pixel(scene, 4, 3, collect_trace=True)
            self.assertGreater(len(rows), 0)
            self.assertTrue(pixel.first_exceed is not None)
            first = pixel.first_exceed
            assert first is not None
            self.assertEqual(first["medium_id"], "medium_a")
            self.assertGreater(first["step"], 0)
            self.assertGreaterEqual(first["segment_optical_depth"], 0.0)
            self.assertGreater(first["accumulated_optical_depth"], 0.0)
            self.assertGreaterEqual(
                first["accumulated_optical_depth"],
                first["segment_optical_depth"] * 0.5,
            )
            self.assertEqual(len(rows[0]), len(TRACE_COLUMNS))

    def test_depth_and_gray_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = load_scene(write_scene(Path(temp) / "case"))
            image = render_blocks_in_memory(scene, collect_trace=True)
            self.assertTrue(all(0 <= value <= 255 for row in image.gray for value in row))
            depths = [value for row in image.depth for value in row if value >= 0.0]
            self.assertTrue(depths)
            self.assertTrue(all(4.0 <= value <= 8.0 for value in depths))
            self.assertGreater(image.stats["steps"], 0)

    def test_concurrency_and_memory_budget_determinism(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scene_path = write_scene(
                root / "base",
                width=12,
                height=8,
                step_size=0.4,
                tile={"width": 4, "height": 4},
                max_workers=4,
                inflight_memory_limit_bytes=2 * budget_for(4, 4, 1),
            )
            scene_multi = load_scene(scene_path)
            scene_serial = dataclasses.replace(
                scene_multi,
                render=dataclasses.replace(scene_multi.render, max_workers=1),
            )
            multi = render_blocks_in_memory(scene_multi, collect_trace=True)
            serial = render_blocks_in_memory(scene_serial, collect_trace=True)
            self.assertEqual(multi.gray, serial.gray)
            self.assertEqual(multi.depth, serial.depth)
            self.assertEqual(multi.trace_rows, serial.trace_rows)
            peak = multi.stats["scheduler"]["peak_inflight_bytes"]
            self.assertLessEqual(peak, scene_multi.render.inflight_memory_limit_bytes)
            budgets = [tile_buffer_bytes(scene_multi, item) for item in tile_geometries(scene_multi)]
            self.assertGreaterEqual(peak, min(budgets))

    def test_trace_every_sample_in_final_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scene = load_scene(write_scene(Path(temp) / "case", width=6, height=4))
            result = run_render(scene, run_stability=False, collect_trace=True)
            self.assertTrue(result.ok, result.payload)
            report = json.loads((scene.render.output_dir / "error_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["trace_columns"], list(TRACE_COLUMNS))
            self.assertGreater(report["summary"]["trace_rows"], 0)
            self.assertTrue((scene.render.output_dir / "error_trace.jsonl.gz").exists())


if __name__ == "__main__":
    unittest.main()

