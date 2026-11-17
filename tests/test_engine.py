from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

import numpy as np

from glyph_raster.engine import RasterEngine
from glyph_raster.errors import EngineError
from glyph_raster.geometry import (
    assemble_tiles,
    build_glyph_geometries,
    build_tile_specs,
    flatten_quadratic,
    rasterize_scene_tiles,
    rasterize_tile,
)
from glyph_raster.model import load_scene_bundle
from glyph_raster.state import read_pgm
from tools.generate_samples import build_sample

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _tile_file_hashes(workspace: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    tiles = workspace / "state" / "sample" / "tiles"
    for path in sorted(tiles.glob("*.json")):
        result[path.stem] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _fresh_full_canvas(workspace: Path, *, apply_edits: bool) -> np.ndarray:
    bundle = load_scene_bundle(
        "samples/scene.json", workspace=workspace, apply_edits=apply_edits
    )
    specs, tiles = rasterize_scene_tiles(bundle.target)
    return assemble_tiles(bundle.target, specs, tiles)


class WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.scene_path, self.glyphs_path = build_sample(self.workspace, "samples")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def render(self, *, apply_edits: bool = False, engine: RasterEngine | None = None):
        bundle = load_scene_bundle(
            "samples/scene.json",
            workspace=self.workspace,
            apply_edits=apply_edits,
        )
        active_engine = engine or RasterEngine(bundle)
        result = active_engine.render()
        return result, active_engine, bundle


class ValidationTests(WorkspaceCase):
    def test_nonfinite_font_value_has_business_code_and_no_outputs(self) -> None:
        scene = _load_json(self.scene_path)
        scene["font"]["unitsPerEm"] = 0
        _write_json(self.scene_path, scene)
        with self.assertRaises(EngineError) as caught:
            load_scene_bundle(
                "samples/scene.json", workspace=self.workspace, apply_edits=False
            )
        self.assertEqual(caught.exception.code, "E_VALIDATION")
        self.assertFalse((self.workspace / "state").exists())
        self.assertFalse((self.workspace / "out").exists())

    def test_open_contour_is_rejected(self) -> None:
        outlines = _load_json(self.glyphs_path)
        outlines["glyphs"][0]["contours"][0]["segments"][-1]["to"] = 2
        _write_json(self.glyphs_path, outlines)
        with self.assertRaises(EngineError) as caught:
            load_scene_bundle(
                "samples/scene.json", workspace=self.workspace, apply_edits=False
            )
        self.assertEqual(caught.exception.code, "E_CONTOUR")
        self.assertFalse((self.workspace / "state").exists())
        self.assertFalse((self.workspace / "out").exists())

    def test_edit_target_validation_happens_before_state_creation(self) -> None:
        scene = _load_json(self.scene_path)
        scene["edits"][0]["glyph"] = "does-not-exist"
        _write_json(self.scene_path, scene)
        with self.assertRaises(EngineError) as caught:
            load_scene_bundle(
                "samples/scene.json", workspace=self.workspace, apply_edits=True
            )
        self.assertEqual(caught.exception.code, "E_EDIT")
        self.assertFalse((self.workspace / "state").exists())
        self.assertFalse((self.workspace / "out").exists())

    def test_bad_curve_index_and_nonfinite_edit_value_are_rejected(self) -> None:
        original_outlines = _load_json(self.glyphs_path)
        outlines = json.loads(json.dumps(original_outlines))
        outlines["glyphs"][0]["contours"][0]["segments"][1]["control"] = 9999
        _write_json(self.glyphs_path, outlines)
        with self.assertRaises(EngineError) as caught:
            load_scene_bundle(
                "samples/scene.json", workspace=self.workspace, apply_edits=True
            )
        self.assertEqual(caught.exception.code, "E_VALIDATION")

        _write_json(self.glyphs_path, original_outlines)
        scene = _load_json(self.scene_path)
        scene["edits"][0]["x"] = "not-finite"
        _write_json(self.scene_path, scene)
        with self.assertRaises(EngineError) as caught:
            load_scene_bundle(
                "samples/scene.json", workspace=self.workspace, apply_edits=True
            )
        self.assertEqual(caught.exception.code, "E_EDIT")
        self.assertFalse((self.workspace / "state").exists())
        self.assertFalse((self.workspace / "out").exists())

    def test_cli_returns_json_error_and_writes_nothing(self) -> None:
        scene = _load_json(self.scene_path)
        scene["font"]["baselineOffset"] = "not-a-number"
        _write_json(self.scene_path, scene)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "glyph_raster",
                "render",
                "--workspace",
                str(self.workspace),
                "--scene",
                "samples/scene.json",
            ],
            cwd=self.workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "E_VALIDATION")
        self.assertFalse((self.workspace / "state").exists())
        self.assertFalse((self.workspace / "out").exists())


class GeometryTests(unittest.TestCase):
    def test_adaptive_quadratic_flattening_stays_within_tolerance(self) -> None:
        start = (0.0, 0.0)
        control = (50.0, 100.0)
        end = (100.0, 0.0)
        tolerance = 0.25
        points = flatten_quadratic(start, control, end, tolerance)
        polyline = [start, *points]

        def segment_distance(point, a, b):
            vx = b[0] - a[0]
            vy = b[1] - a[1]
            wx = point[0] - a[0]
            wy = point[1] - a[1]
            length_squared = vx * vx + vy * vy
            if length_squared == 0:
                return math.hypot(wx, wy)
            t = max(0.0, min(1.0, (wx * vx + wy * vy) / length_squared))
            return math.hypot(point[0] - (a[0] + t * vx), point[1] - (a[1] + t * vy))

        maximum_error = 0.0
        for index in range(2001):
            t = index / 2000.0
            point = (
                (1 - t) ** 2 * start[0]
                + 2 * (1 - t) * t * control[0]
                + t**2 * end[0],
                (1 - t) ** 2 * start[1]
                + 2 * (1 - t) * t * control[1]
                + t**2 * end[1],
            )
            distance = min(
                segment_distance(point, polyline[i], polyline[i + 1])
                for i in range(len(polyline) - 1)
            )
            maximum_error = max(maximum_error, distance)
        self.assertLessEqual(maximum_error, tolerance)

    def test_depth_cap_is_deterministic_chord_fallback(self) -> None:
        first = flatten_quadratic(
            (0.0, 0.0), (50.0, 100.0), (100.0, 0.0), 0.0, max_depth=5
        )
        second = flatten_quadratic(
            (0.0, 0.0), (50.0, 100.0), (100.0, 0.0), 0.0, max_depth=5
        )
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 2**5)
        self.assertEqual(first[-1], (100.0, 0.0))


class RasterReferenceTests(WorkspaceCase):
    def test_scanline_tile_matches_scalar_per_sample_winding(self) -> None:
        bundle = load_scene_bundle(
            "samples/scene.json", workspace=self.workspace, apply_edits=False
        )
        scene = bundle.target
        geometries = build_glyph_geometries(scene)
        specs = build_tile_specs(scene, geometries)
        geometry_map = {geometry.glyph_id: geometry for geometry in geometries}
        selected_spec = None
        actual = None
        for spec in specs:
            pixels = rasterize_tile(scene, spec, geometries)
            if np.any(pixels):
                selected_spec = spec
                actual = pixels
                break
        self.assertIsNotNone(selected_spec)
        assert selected_spec is not None and actual is not None

        polygons = []
        for glyph_id in selected_spec.glyph_ids:
            polygons.extend(geometry_map[glyph_id].polygons)
        supersampling = scene.supersampling
        counts = np.zeros((selected_spec.height, selected_spec.width), dtype=np.int32)

        def winding_at(x: float, y: float) -> int:
            winding = 0
            for polygon in polygons:
                for index in range(len(polygon)):
                    x1, y1 = polygon[index]
                    x2, y2 = polygon[(index + 1) % len(polygon)]
                    if y1 <= y < y2 and (x2 - x1) * (y - y1) - (x - x1) * (
                        y2 - y1
                    ) > 0:
                        winding += 1
                    elif y2 <= y < y1 and (x2 - x1) * (y - y1) - (x - x1) * (
                        y2 - y1
                    ) < 0:
                        winding -= 1
            return winding

        for pixel_y in range(selected_spec.height):
            for pixel_x in range(selected_spec.width):
                hits = 0
                for sample_y in range(supersampling):
                    y = (
                        selected_spec.y0
                        + pixel_y
                        + (sample_y + 0.5) / supersampling
                    )
                    for sample_x in range(supersampling):
                        x = (
                            selected_spec.x0
                            + pixel_x
                            + (sample_x + 0.5) / supersampling
                        )
                        if winding_at(x, y) != 0:
                            hits += 1
                denominator = supersampling * supersampling
                counts[pixel_y, pixel_x] = (hits * 255 + denominator // 2) // denominator

        np.testing.assert_array_equal(actual, counts.astype(np.uint8))


class IncrementalTests(WorkspaceCase):
    def test_incremental_matches_full_and_exactly_reuses_unaffected_tiles(self) -> None:
        base_result, _, _ = self.render(apply_edits=False)
        self.assertEqual(base_result["code"], "OK")
        before_hashes = _tile_file_hashes(self.workspace)

        base_bundle = load_scene_bundle(
            "samples/scene.json", workspace=self.workspace, apply_edits=False
        )
        target_bundle = load_scene_bundle(
            "samples/scene.json", workspace=self.workspace, apply_edits=True
        )
        base_specs = build_tile_specs(base_bundle.target)
        target_specs = build_tile_specs(target_bundle.target)
        expected_changed = {
            base_spec.key
            for base_spec, target_spec in zip(base_specs, target_specs)
            if base_spec.version != target_spec.version
        }
        self.assertGreater(len(expected_changed), 0)
        self.assertLess(len(expected_changed), len(target_specs))

        incremental_result, _, _ = self.render(apply_edits=True)
        self.assertEqual(incremental_result["code"], "OK")
        self.assertEqual(incremental_result["mode"], "incremental")
        self.assertEqual(
            incremental_result["tiles"]["invalidated"], len(expected_changed)
        )
        self.assertEqual(
            incremental_result["tiles"]["reused"],
            len(target_specs) - len(expected_changed),
        )
        self.assertGreater(incremental_result["tiles"]["archived"], 0)
        self.assertIn("geometry_bbox", incremental_result["invalidReasons"])
        self.assertIn("size", incremental_result["invalidReasons"])
        self.assertIn("x_offset", incremental_result["invalidReasons"])
        self.assertIn("layout_shift", incremental_result["invalidReasons"])

        after_hashes = _tile_file_hashes(self.workspace)
        unchanged = set(before_hashes) - expected_changed
        for filename in unchanged:
            if filename.endswith(".json") and filename.startswith("tile_"):
                self.assertEqual(before_hashes[filename], after_hashes[filename])

        expected = _fresh_full_canvas(self.workspace, apply_edits=True)
        actual = read_pgm(self.workspace / "out" / "sample.pgm").pixels
        np.testing.assert_array_equal(actual, expected)
        self.assertEqual(
            read_pgm(self.workspace / "out" / "sample.pgm").info.content_version,
            target_bundle.target.content_version(),
        )

        reuse_result, _, _ = self.render(apply_edits=True)
        self.assertEqual(reuse_result["mode"], "reuse")
        self.assertEqual(reuse_result["tiles"]["rendered"], 0)
        self.assertEqual(reuse_result["tiles"]["reused"], len(target_specs))
        self.assertFalse(reuse_result["bitmap"]["replaced"])


class PersistenceAndDigestTests(WorkspaceCase):
    def test_second_run_reuses_every_tile(self) -> None:
        first, _, _ = self.render()
        self.assertEqual(first["code"], "OK")
        output_path = self.workspace / "out" / "sample.pgm"
        first_output = output_path.read_bytes()
        second, _, _ = self.render()
        self.assertEqual(second["code"], "OK")
        self.assertEqual(second["tiles"]["reused"], second["tiles"]["total"])
        self.assertEqual(second["tiles"]["rendered"], 0)
        self.assertFalse(second["bitmap"]["replaced"])
        self.assertEqual(output_path.read_bytes(), first_output)

    def test_pixel_digest_corruption_returns_e_digest_and_repairs_one_tile(self) -> None:
        self.render()
        tile_path = next((self.workspace / "state/sample/tiles").glob("*.json"))
        payload = _load_json(tile_path)
        raw = bytearray(zlib.decompress(base64.b64decode(payload["pixels"])))
        raw[len(raw) // 2] ^= 0x5A
        payload["pixels"] = base64.b64encode(zlib.compress(bytes(raw), 9)).decode("ascii")
        _write_json(tile_path, payload)

        repaired, _, _ = self.render()
        self.assertEqual(repaired["code"], "E_DIGEST")
        self.assertEqual(repaired["status"], "recovered")
        self.assertEqual(repaired["tiles"]["rendered"], 1)
        self.assertEqual(repaired["tiles"]["reused"], repaired["tiles"]["total"] - 1)
        self.assertEqual(len(repaired["digestIssues"]), 1)
        expected = _fresh_full_canvas(self.workspace, apply_edits=False)
        actual = read_pgm(self.workspace / "out" / "sample.pgm").pixels
        np.testing.assert_array_equal(actual, expected)

        clean, _, _ = self.render()
        self.assertEqual(clean["code"], "OK")
        self.assertEqual(clean["tiles"]["rendered"], 0)

    def test_missing_tile_after_completed_manifest_returns_e_digest(self) -> None:
        self.render()
        tile_path = next((self.workspace / "state/sample/tiles").glob("*.json"))
        tile_path.unlink()
        repaired, _, _ = self.render()
        self.assertEqual(repaired["code"], "E_DIGEST")
        self.assertEqual(repaired["tiles"]["rendered"], 1)
        self.assertEqual(repaired["digestIssues"][0]["reason"], "missing")
        expected = _fresh_full_canvas(self.workspace, apply_edits=False)
        actual = read_pgm(self.workspace / "out" / "sample.pgm").pixels
        np.testing.assert_array_equal(actual, expected)

    def test_pixel_count_mismatch_is_rejected(self) -> None:
        self.render()
        tile_path = next((self.workspace / "state/sample/tiles").glob("*.json"))
        payload = _load_json(tile_path)
        payload["pixelCount"] += 1
        _write_json(tile_path, payload)
        repaired, _, _ = self.render()
        self.assertEqual(repaired["code"], "E_DIGEST")
        self.assertEqual(repaired["tiles"]["rendered"], 1)


class InterruptionTests(WorkspaceCase):
    def test_interrupt_only_at_tile_boundary_and_resume(self) -> None:
        bundle = load_scene_bundle(
            "samples/scene.json", workspace=self.workspace, apply_edits=False
        )
        completed_callbacks = 0
        holder: dict[str, RasterEngine] = {}

        def on_tile(spec) -> None:
            nonlocal completed_callbacks
            completed_callbacks += 1
            if completed_callbacks == 1:
                holder["engine"].request_stop(signal.SIGTERM)

        engine = RasterEngine(bundle, on_tile=on_tile)
        holder["engine"] = engine
        result = engine.render()
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["exitCode"], 143)
        marker = self.workspace / "state/sample/interrupted.json"
        self.assertTrue(marker.exists())
        marker_payload = _load_json(marker)
        self.assertEqual(marker_payload["exitCode"], 143)
        self.assertFalse((self.workspace / "out" / "sample.pgm").exists())
        self.assertFalse((self.workspace / "out" / "sample-report.json").exists())
        persisted = len(list((self.workspace / "state/sample/tiles").glob("*.json")))
        self.assertGreaterEqual(persisted, 1)

        resumed, _, _ = self.render()
        self.assertEqual(resumed["code"], "OK")
        self.assertEqual(resumed["tiles"]["reused"], persisted)
        self.assertFalse(marker.exists())
        expected = _fresh_full_canvas(self.workspace, apply_edits=False)
        actual = read_pgm(self.workspace / "out" / "sample.pgm").pixels
        np.testing.assert_array_equal(actual, expected)


class ConcurrencyTests(WorkspaceCase):
    def _variant(self, name: str, concurrency: int, memory_limit: int) -> Path:
        scene = _load_json(self.scene_path)
        scene["concurrency"] = concurrency
        scene["memoryLimitBytes"] = memory_limit
        scene["stateDir"] = f"state/{name}"
        scene["output"] = f"out/{name}.pgm"
        scene["report"] = f"out/{name}-report.json"
        path = self.workspace / "samples" / f"scene-{name}.json"
        _write_json(path, scene)
        return path

    def test_concurrency_and_memory_cap_preserve_identical_pixels(self) -> None:
        tile_bytes = 48 * 48 * 4**2
        serial_path = self._variant("serial", 1, tile_bytes * 4)
        parallel_path = self._variant("parallel", 4, tile_bytes * 2)

        serial_bundle = load_scene_bundle(
            serial_path, workspace=self.workspace, apply_edits=False
        )
        serial_result = RasterEngine(serial_bundle).render()
        self.assertEqual(serial_result["code"], "OK")

        import glyph_raster.engine as engine_module

        original_rasterize = engine_module.rasterize_tile
        lock = __import__("threading").Lock()
        active = 0
        active_bytes = 0
        maximum_active = 0
        maximum_bytes = 0
        current_limit = tile_bytes * 2

        def instrumented(scene, spec, geometries):
            nonlocal active, active_bytes, maximum_active, maximum_bytes
            with lock:
                active += 1
                active_bytes += spec.width * spec.height * scene.supersampling**2
                maximum_active = max(maximum_active, active)
                maximum_bytes = max(maximum_bytes, active_bytes)
            try:
                time.sleep(0.002)
                return original_rasterize(scene, spec, geometries)
            finally:
                with lock:
                    active -= 1
                    active_bytes -= spec.width * spec.height * scene.supersampling**2

        parallel_bundle = load_scene_bundle(
            parallel_path, workspace=self.workspace, apply_edits=False
        )
        with mock.patch.object(engine_module, "rasterize_tile", instrumented):
            parallel_result = RasterEngine(parallel_bundle).render()
        self.assertEqual(parallel_result["code"], "OK")
        self.assertLessEqual(maximum_active, 2)
        self.assertLessEqual(maximum_bytes, current_limit)
        self.assertGreaterEqual(maximum_active, 2)

        serial_pixels = read_pgm(self.workspace / "out" / "serial.pgm").pixels
        parallel_pixels = read_pgm(self.workspace / "out" / "parallel.pgm").pixels
        np.testing.assert_array_equal(serial_pixels, parallel_pixels)

    def test_manual_sigterm_reaches_interrupted_exit_code(self) -> None:
        scene = _load_json(self.scene_path)
        scene["concurrency"] = 1
        scene["memoryLimitBytes"] = 48 * 48 * 4**2
        scene["stateDir"] = "state/signal"
        scene["output"] = "out/signal.pgm"
        scene["report"] = "out/signal-report.json"
        signal_scene = self.workspace / "samples" / "scene-signal.json"
        _write_json(signal_scene, scene)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "glyph_raster",
                "render",
                "--workspace",
                str(self.workspace),
                "--scene",
                "samples/scene-signal.json",
            ],
            cwd=self.workspace,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.45)
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 143, msg=f"stdout={stdout} stderr={stderr}")
        payload = json.loads(stdout)
        self.assertEqual(payload["code"], "E_INTERRUPTED")
        self.assertTrue((self.workspace / "state/signal/interrupted.json").exists())
        self.assertFalse((self.workspace / "out/signal.pgm").exists())


if __name__ == "__main__":
    unittest.main()
