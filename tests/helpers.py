from __future__ import annotations

import copy
import json
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pointcloud_workbench.binary_formats import read_pfm, read_png_gray
from pointcloud_workbench.pipeline import RenderOptions, render_task
from pointcloud_workbench.samplegen import generate_samples


class SampleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pcwb-test-")
        self.root = Path(self.temp.name)
        self.samples = self.root / "samples"
        self.manifest = generate_samples(self.samples)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def task_document(self, name: str = "task_base.json") -> dict[str, Any]:
        return json.loads((self.samples / name).read_text(encoding="utf-8"))

    def write_task(self, document: dict[str, Any], name: str) -> Path:
        path = self.samples / name
        path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def render(
        self,
        task_path: Path,
        *,
        state_name: str,
        output_name: str,
        verify_full: bool = False,
        stop_event: threading.Event | None = None,
        after_block: Callable[[int, str], None] | None = None,
        clock: Any = None,
    ) -> dict[str, Any]:
        return render_task(
            task_path,
            RenderOptions(
                state_root=self.root / state_name,
                output_root=self.root / output_name,
                verify_full=verify_full,
                stop_event=stop_event,
                after_block_persisted=after_block,
                logical_clock=clock,
            ),
        )

    def camera_ids(self) -> list[str]:
        return [
            camera["id"]
            for camera in json.loads(
                (self.samples / "camera_params.json").read_text(encoding="utf-8")
            )["cameras"]
        ]

    def read_images(self, output_name: str) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        result = {}
        for camera_id in self.camera_ids():
            root = self.root / output_name / camera_id
            result[camera_id] = (
                read_png_gray(root / "grayscale.png"),
                read_pfm(root / "depth.pfm"),
            )
        return result

    def modify_point_record(
        self,
        task_path: Path,
        point_index: int,
        float_offset: int,
        value: float,
    ) -> None:
        document = json.loads(task_path.read_text(encoding="utf-8"))
        cloud_path = self.samples / document["pointcloud"]["path"]
        data = bytearray(cloud_path.read_bytes())
        record_offset = 24 + point_index * 32 + float_offset * 4
        struct.pack_into("<f", data, record_offset, value)
        cloud_path.write_bytes(bytes(data))

    def clone_task(
        self,
        source_name: str,
        target_name: str,
        *,
        mutate: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        document = copy.deepcopy(self.task_document(source_name))
        if mutate is not None:
            mutate(document)
        return self.write_task(document, target_name)


def assert_images_equal(
    case: unittest.TestCase,
    left: dict[str, tuple[np.ndarray, np.ndarray]],
    right: dict[str, tuple[np.ndarray, np.ndarray]],
) -> None:
    case.assertEqual(set(left), set(right))
    for camera_id in left:
        np.testing.assert_array_equal(left[camera_id][0], right[camera_id][0])
        np.testing.assert_array_equal(left[camera_id][1], right[camera_id][1])

