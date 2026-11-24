from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from pointcloud_workbench.samplegen import SAMPLE_CACHE_KEY
from pointcloud_workbench.spec import load_task
from pointcloud_workbench.state import inspect_block

from tests.helpers import SampleTest


def _read_block(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    with np.load(path, allow_pickle=False) as archive:
        gray = np.array(archive["gray"], copy=True)
        depth = np.array(archive["depth"], copy=True)
        dependencies = np.array(archive["dependencies"], copy=True)
        meta = json.loads(np.asarray(archive["meta"], dtype=np.uint8).tobytes().decode("utf-8"))
    return gray, depth, dependencies, meta


def _write_block(
    path: Path,
    gray: np.ndarray,
    depth: np.ndarray,
    dependencies: np.ndarray,
    meta: dict,
) -> None:
    meta_bytes = json.dumps(
        meta,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    np.savez(
        path,
        gray=gray,
        depth=depth,
        dependencies=dependencies,
        meta=np.frombuffer(meta_bytes, dtype=np.uint8),
    )


class PersistenceTests(SampleTest):
    def test_block_metadata_contains_dependencies_and_version(self) -> None:
        self.render(
            self.samples / "task_base.json",
            state_name="state_cache",
            output_name="out",
        )
        blocks_root = (
            self.root / "state_cache" / SAMPLE_CACHE_KEY / "cam_front" / "blocks"
        )
        block_path = None
        dependencies = np.empty(0)
        meta = {}
        for candidate in sorted(blocks_root.glob("b*.npz")):
            _, _, candidate_dependencies, candidate_meta = _read_block(candidate)
            if candidate_dependencies.size > dependencies.size:
                block_path = candidate
                dependencies = candidate_dependencies
                meta = candidate_meta
        self.assertIsNotNone(block_path)
        self.assertTrue(dependencies.size > 0)
        for field in (
            "version_id",
            "dataset_digest",
            "camera_digest",
            "render_digest",
            "dependency_digest",
            "content_digest",
            "declared_pixel_count",
        ):
            self.assertIn(field, meta)
        task = load_task(self.samples / "task_base.json")
        block_index = int(meta["block_index"])
        inspection = inspect_block(
            block_path,
            task.render.blocks()[block_index],
            expected_version=meta["version_id"],
        )
        self.assertTrue(inspection.content_ok)
        self.assertEqual(inspection.reasons, [])

    def test_pixel_count_tampering_is_detected(self) -> None:
        self.render(
            self.samples / "task_base.json",
            state_name="state_cache",
            output_name="out",
        )
        block_path = (
            self.root / "state_cache" / SAMPLE_CACHE_KEY / "cam_front" / "blocks" / "b0002.npz"
        )
        gray, depth, dependencies, meta = _read_block(block_path)
        meta["declared_pixel_count"] = 1
        _write_block(block_path, gray, depth, dependencies, meta)
        task = load_task(self.samples / "task_base.json")
        inspection = inspect_block(block_path, task.render.blocks()[2])
        self.assertIn("PIXEL_COUNT_MISMATCH", inspection.reason_codes())
        self.assertFalse(inspection.content_ok)

    def test_content_tampering_is_detected(self) -> None:
        self.render(
            self.samples / "task_base.json",
            state_name="state_cache",
            output_name="out",
        )
        block_path = (
            self.root / "state_cache" / SAMPLE_CACHE_KEY / "cam_front" / "blocks" / "b0003.npz"
        )
        gray, depth, dependencies, meta = _read_block(block_path)
        gray[0, 0] = (int(gray[0, 0]) + 1) % 256
        _write_block(block_path, gray, depth, dependencies, meta)
        task = load_task(self.samples / "task_base.json")
        inspection = inspect_block(block_path, task.render.blocks()[3])
        self.assertIn("DIGEST_MISMATCH", inspection.reason_codes())
        self.assertFalse(inspection.content_ok)

