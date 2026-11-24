from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from terrain_baker import cli
from terrain_baker.cache import ChunkCache
from terrain_baker.errors import BakeInterrupted, SchemaError
from terrain_baker.heightfield import GlobalHeightField
from terrain_baker.io_utils import atomic_write_json
from terrain_baker.outputs import publish_file
from terrain_baker.pipeline import bake_scene
from terrain_baker.sample_generator import apply_edit_to_directory, generate_samples
from terrain_baker.schema import load_edit_file, load_scene_file, validate_edit_dict
from terrain_baker.scheduler import BoundarySignal, run_bounded_chunks


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TerrainBakerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory(prefix="terrain-baker-test-")
        cls.tmp = Path(cls.temporary_directory.name)
        cls.samples = generate_samples(cls.tmp / "samples")
        cls.sample_dir = Path(cls.samples["sample_dir"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def _copy_sample(self, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(self.sample_dir, target)
        return target

    def _load(self, sample_dir: Path, scene_name: str = "scene.initial.json"):
        scene = load_scene_file(sample_dir / scene_name)
        heightfield = GlobalHeightField.from_scene(scene)
        return scene, heightfield

    def test_generated_scene_is_valid(self) -> None:
        scene, heightfield = self._load(self.sample_dir)
        self.assertEqual((heightfield.rows, heightfield.cols), (128, 128))
        self.assertEqual(len(scene["heightfield"]["tiles"]), 16)
        self.assertTrue(np.all(np.isfinite(heightfield.heights_world)))

    def test_schema_rejects_coincident_camera_with_json_error_and_no_output(self) -> None:
        bad_scene_path = self.tmp / "bad_camera.json"
        scene = json.loads((self.sample_dir / "scene.initial.json").read_text(encoding="utf-8"))
        scene["camera"]["target"] = list(scene["camera"]["position"])
        bad_scene_path.write_text(json.dumps(scene), encoding="utf-8")
        output_dir = self.tmp / "must_not_exist_camera"

        return_code = cli.main(
            [
                "bake",
                "--scene",
                str(bad_scene_path),
                "--output-dir",
                str(output_dir),
            ]
        )
        self.assertEqual(return_code, 2)
        self.assertFalse(output_dir.exists())

    def test_schema_rejects_out_of_bounds_integer_edit(self) -> None:
        scene = load_scene_file(self.sample_dir / "scene.initial.json")
        with self.assertRaises(SchemaError) as context:
            validate_edit_dict(
                {
                    "region": {
                        "row_start": 120,
                        "row_end_exclusive": 129,
                        "col_start": 0,
                        "col_end_exclusive": 8,
                    }
                },
                scene["heightfield"],
            )
        self.assertEqual(context.exception.code, "E_SCHEMA")

    def test_memory_gate_renders_in_index_order(self) -> None:
        chunks = [
            SimpleNamespace(index=i, chunk_id=f"c{i:02d}")
            for i in range(6)
        ]
        lock = threading.Lock()
        active = 0
        max_active = 0
        start_order = []

        def task(chunk):
            nonlocal active, max_active
            with lock:
                start_order.append(chunk.index)
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return chunk.index

        outcome = run_bounded_chunks(
            chunks,
            max_workers=4,
            memory_budget_bytes=1,
            buffer_size_func=lambda chunk: 1,
            task_func=task,
        )
        self.assertIsNone(outcome.first_error)
        self.assertEqual(sorted(outcome.successes), [0, 1, 2, 3, 4, 5])
        self.assertEqual(start_order, [0, 1, 2, 3, 4, 5])
        self.assertLessEqual(outcome.max_active_chunks, 1)
        self.assertEqual(max_active, 1)

    def test_scheduler_preserves_first_error_and_successes(self) -> None:
        chunks = [SimpleNamespace(index=i, chunk_id=f"e{i}") for i in range(4)]

        def task(chunk):
            if chunk.index == 1:
                raise RuntimeError("boom-first")
            if chunk.index == 0:
                time.sleep(0.05)
            return chunk.index

        outcome = run_bounded_chunks(
            chunks,
            max_workers=2,
            memory_budget_bytes=2,
            buffer_size_func=lambda chunk: 1,
            task_func=task,
        )
        self.assertIsNotNone(outcome.first_error)
        self.assertIn("boom-first", str(outcome.first_error))
        self.assertIn(0, outcome.successes)
        self.assertTrue(outcome.errors and outcome.errors[0]["message"] == "boom-first")

    def test_local_edit_invalidation_serial_equivalence_and_archive(self) -> None:
        sample_dir = self._copy_sample("local_edit_case")
        output_dir = self.tmp / "local_edit_out"
        state_dir = self.tmp / "local_edit_state"

        scene, heightfield = self._load(sample_dir)
        baseline = bake_scene(
            scene,
            heightfield,
            output_dir=output_dir,
            state_dir=state_dir,
            mode="initial",
            run_reference=False,
            run_id="test-baseline",
        )
        self.assertEqual(baseline["counts"]["rerendered_chunks"], 12)

        apply_edit_to_directory(sample_dir)
        edited_scene = load_scene_file(sample_dir / "scene.local_edit.json")
        edited_hf = GlobalHeightField.from_scene(edited_scene)
        edit = load_edit_file(sample_dir / "edits" / "local_ridge_valley.json", edited_scene)
        report = bake_scene(
            edited_scene,
            edited_hf,
            output_dir=output_dir,
            state_dir=state_dir,
            report_path=self.tmp / "local_report.json",
            mode="terrain_edit",
            edit=edit,
            run_reference=True,
            run_id="test-edit",
        )

        self.assertEqual(report["counts"]["total_chunks"], 12)
        self.assertEqual(report["counts"]["rerendered_chunks"], 4)
        self.assertEqual(report["counts"]["reused_chunks"], 8)
        self.assertTrue(report["seam_validation"]["pixel_exact"])
        self.assertEqual(report["seam_validation"]["gray_pixel_max_abs_diff"], 0)
        self.assertEqual(report["seam_validation"]["depth_world_max_abs_diff"], 0.0)
        self.assertEqual(
            report["seam_validation"]["boundary_band_normal_vector_max_diff_vs_full"], 0.0
        )
        self.assertEqual(
            report["seam_validation"][
                "heightfield_ownership_same_point_max_normal_deviation"
            ],
            0.0,
        )
        self.assertEqual(sum(1 for chunk in report["chunks"] if chunk.get("archive")), 4)

        serial_out = self.tmp / "serial_out"
        serial_state = self.tmp / "serial_state"
        serial_report = bake_scene(
            edited_scene,
            edited_hf,
            output_dir=serial_out,
            state_dir=serial_state,
            mode="initial",
            max_workers_override=1,
            run_reference=False,
            run_id="test-serial",
        )
        self.assertEqual(serial_report["scheduler"]["bounded_pool_workers"], 1)

        depth_concurrent = np.load(output_dir / "terrain_depth.npy")
        depth_serial = np.load(serial_out / "terrain_depth.npy")
        height_concurrent = np.load(output_dir / "terrain_height.npy")
        height_serial = np.load(serial_out / "terrain_height.npy")
        self.assertTrue(np.array_equal(depth_concurrent, depth_serial, equal_nan=True))
        self.assertTrue(np.array_equal(height_concurrent, height_serial, equal_nan=True))

    def test_interrupt_boundary_and_digest_recovery(self) -> None:
        sample_dir = self._copy_sample("interrupt_case")
        output_dir = self.tmp / "interrupt_out"
        state_dir = self.tmp / "interrupt_state"
        scene, heightfield = self._load(sample_dir)
        token = BoundarySignal()

        with self.assertRaises(BakeInterrupted):
            bake_scene(
                scene,
                heightfield,
                output_dir=output_dir,
                state_dir=state_dir,
                mode="initial",
                signal_token=token,
                stop_after_completed=1,
                run_reference=False,
                run_id="interrupt",
            )
        marker = state_dir / "INTERRUPTED.json"
        self.assertTrue(marker.exists())
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_data["exit_code"], 130)
        self.assertGreaterEqual(len(marker_data["completed_chunk_ids"]), 1)

        corrupt_metadata = state_dir / "chunks" / "chunk_r001_c001.json"
        corrupt_metadata.write_text("{broken", encoding="utf-8")
        report = bake_scene(
            scene,
            heightfield,
            output_dir=output_dir,
            state_dir=state_dir,
            mode="initial",
            run_reference=False,
            run_id="restart",
        )
        self.assertEqual(report["status"], "success")
        self.assertGreaterEqual(report["counts"]["reused_chunks"], 1)
        self.assertTrue(
            any(c["chunk_id"] == "chunk_r001_c001" and c["invalid"] for c in report["chunks"])
        )

    def test_atomic_output_version_skips_same_version(self) -> None:
        import io

        buffer = io.BytesIO()
        np.save(buffer, np.ones((2, 2), dtype=np.float64))
        payload_v1 = buffer.getvalue()
        buffer = io.BytesIO()
        np.save(buffer, np.zeros((2, 2), dtype=np.float64))
        payload_v2 = buffer.getvalue()

        path = self.tmp / "output.npy"
        first = publish_file(path, payload_v1, version="v1", dimensions=(2, 2), kind="test")
        second = publish_file(path, payload_v1, version="v1", dimensions=(2, 2), kind="test")
        third = publish_file(path, payload_v2, version="v2", dimensions=(2, 2), kind="test")
        self.assertEqual(first["action"], "atomic_replaced")
        self.assertEqual(second["action"], "kept_same_version")
        self.assertEqual(third["action"], "atomic_replaced")
        self.assertTrue(np.array_equal(np.load(path), np.zeros((2, 2))))

    def test_cache_rejects_tampered_payload(self) -> None:
        state_dir = self.tmp / "cache_tamper"
        with ChunkCache(state_dir) as cache:
            arrays = {
                "gray": np.zeros((2, 2), dtype=np.uint8),
                "shade": np.zeros((2, 2), dtype=np.float64),
                "depth": np.zeros((2, 2), dtype=np.float64),
                "height": np.zeros((2, 2), dtype=np.float64),
                "normal": np.zeros((2, 2, 3), dtype=np.float64),
                "hit": np.zeros((2, 2), dtype=bool),
            }
            cache.save_chunk(
                chunk_id="chunk_x",
                chunk_index=0,
                global_version="g1",
                dependency_digest="d1",
                dependency={"height_range": {}},
                arrays=arrays,
            )
            payload_path, _ = cache.paths_for("chunk_x")
            data = bytearray(payload_path.read_bytes())
            data[20] ^= 0x01
            payload_path.write_bytes(bytes(data))
            self.assertIsNone(cache.load_chunk("chunk_x"))


if __name__ == "__main__":
    unittest.main()
