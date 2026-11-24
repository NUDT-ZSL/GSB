from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pointcloud_workbench.viewer import build_static_viewer, launch_tk_viewer

from tests.helpers import SampleTest


class ViewerTests(SampleTest):
    def test_static_viewer_is_offline_interactive_and_reads_persisted_data(self) -> None:
        self.render(
            self.samples / "task_base.json",
            state_name="state_shared",
            output_name="out_base",
        )
        report = self.render(
            self.samples / "task_edited.json",
            state_name="state_shared",
            output_name="out_edited",
        )
        output_root = self.root / "out_edited"
        block_files = [Path(camera["outputs"]["blocks"]) for camera in report["cameras"]]
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in block_files
        }
        viewer_path = build_static_viewer(output_root)
        after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in block_files
        }
        self.assertEqual(before, after)
        html = viewer_path.read_text(encoding="utf-8")
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        for camera_id in self.camera_ids():
            self.assertIn(camera_id, html)
        self.assertIn("data:image/png;base64,", html)
        self.assertIn("EDIT_DEPENDENCY_CHANGED", html)
        self.assertIn("dependency_indices", html)
        self.assertIn("digest_ok", html)
        self.assertIn("depth_summary", html)
        self.assertIn(".block.invalid", html)
        self.assertIn('" invalid"', html)
        payload_text = html.split("const DATA = ", 1)[1].split(";\nlet activeCamera", 1)[0]
        payload = json.loads(payload_text)
        self.assertEqual(len(payload["cameras"]), 3)
        for camera in payload["cameras"]:
            both = set()
            for block in camera["blocks"]:
                both.add(block["status"] in {"reused", "reused_promoted"})
            self.assertIn(True, both)
            self.assertIn(False, both)

    def test_viewer_rejects_incomplete_persisted_output(self) -> None:
        self.render(
            self.samples / "task_base.json",
            state_name="state_viewer",
            output_name="out_viewer",
        )
        (self.root / "out_viewer" / "cam_front" / "grayscale.png").unlink()
        from pointcloud_workbench.errors import BusinessError

        with self.assertRaises(BusinessError) as caught:
            build_static_viewer(self.root / "out_viewer")
        self.assertEqual(caught.exception.code, "E_VIEW")

    def test_local_tk_viewer_starts_from_persisted_output(self) -> None:
        try:
            import tkinter as tk

            probe = tk.Tk()
            probe.destroy()
        except Exception as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.render(
            self.samples / "task_base.json",
            state_name="state_tk",
            output_name="out_tk",
        )
        launch_tk_viewer(self.root / "out_tk", auto_close_ms=50)

