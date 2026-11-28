from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from narrative_workbench.compare import compare_all, compare_normalized
from narrative_workbench.generator import generate_project
from narrative_workbench.independent import verify_frames
from narrative_workbench.models import matrix_from_rotation_scale, decompose_covariance
from narrative_workbench.renderer import (
    load_normalized_versions,
    render_all,
    write_normalized_results,
)
from narrative_workbench.storage import read_json


class NarrativeWorkbenchTests(unittest.TestCase):
    def _small_project(self, root: Path, frames: int = 3):
        return generate_project(root, seed=77, frame_count=frames, width=64, height=42)

    def test_covariance_decomposition_round_trip(self) -> None:
        rotation = np.array(
            [
                [0.81379768, -0.46984631, 0.34202014],
                [0.46984631, 0.88256412, 0.0],
                [-0.34202014, 0.0, 0.93969262],
            ],
            dtype=np.float64,
        )
        scale = np.array([0.8, 0.42, 0.09])
        covariance = matrix_from_rotation_scale(rotation, scale)
        axes, recovered_scale, quaternion = decompose_covariance(covariance)
        reconstructed = axes @ np.diag(recovered_scale**2) @ axes.T
        self.assertLess(float(np.max(np.abs(reconstructed - covariance))), 1.0e-12)
        self.assertGreaterEqual(quaternion[0], 0.0)
        self.assertEqual(len(recovered_scale), 3)

    def test_schema_normalization_and_audit_issues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._small_project(root)
            _, normalized = load_normalized_versions(project)
            self.assertEqual(
                {version: len(result.scene.elements) for version, result in normalized.items()},
                {"v1": 15, "v2": 15, "v3": 15},
            )
            self.assertEqual(len(normalized["v1"].issues), 1)
            self.assertEqual(len(normalized["v2"].issues), 1)
            self.assertEqual(len(normalized["v3"].issues), 1)
            self.assertIn("center", normalized["v1"].issues[0].message)
            self.assertIn("4 components", normalized["v2"].issues[0].message)
            self.assertIn("does not map", normalized["v3"].issues[0].message)
            write_normalized_results(project, normalized)
            report, per_version = compare_normalized(project)
            self.assertTrue(report["all_valid_elements_equivalent"])
            self.assertEqual(len(per_version["v2"]), 0)
            self.assertEqual(len(per_version["v3"]), 0)
            self.assertIn("audit_v3_unmapped_layer", report["schema_audit_issue_summary"]["v3"]["element_ids"])

    def test_render_reference_composite_and_independent_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = render_all(
                root,
                seed=91,
                frame_count=3,
                width=64,
                height=42,
                versions=("v1", "v2", "v3"),
            )
            for version in ("v1", "v2", "v3"):
                self.assertTrue(session["versions"][version]["completed"])
            for version in ("v1", "v2", "v3"):
                for index in range(3):
                    frame = read_json(root / "frames" / version / f"frame_{index:04d}.json")
                    self.assertTrue(frame["projection"]["reference_check"]["passed"])
                    self.assertTrue(frame["checks"]["all_composite_values_finite"])
                    self.assertEqual(frame["checks"]["undefined_contribution_count"], 0)
                    arrays = np.load(root / "frames" / version / f"frame_{index:04d}.npz")
                    self.assertEqual(arrays["rgba"].shape, (42, 64, 4))
                    self.assertTrue(np.isfinite(arrays["rgba"]).all())
            verification = verify_frames(generate_project(root, seed=91, frame_count=3, width=64, height=42))
            self.assertTrue(verification["passed"], verification["failures"])
            self.assertEqual(verification["frame_count"], 9)
            comparison = compare_all(
                generate_project(root, seed=91, frame_count=3, width=64, height=42)
            )
            self.assertTrue(comparison["all_frames_pixel_identical"])
            self.assertTrue(comparison["all_draw_orders_identical"])
            self.assertEqual(comparison["normalization_difference_count"], 0)
            self.assertEqual(comparison["sorting_strategy_difference_count"], 0)

    def test_resume_starts_after_last_committed_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = render_all(
                root,
                seed=55,
                frame_count=5,
                width=48,
                height=32,
                versions=("v1",),
                max_new_frames=2,
            )
            self.assertEqual(first["versions"]["v1"]["next_frame"], 2)
            self.assertEqual(first["versions"]["v1"]["rendered_frames_this_run"], [0, 1])
            checkpoint_path = root / "frames" / "v1" / "checkpoint.json"
            before = read_json(checkpoint_path)
            committed_hash = before["committed_frames"][0]["json_sha256"]

            orphan = root / "frames" / "v1" / "frame_0002.json"
            orphan.write_text('{"orphan": true}\n', encoding="utf-8")

            second = render_all(
                root,
                seed=55,
                frame_count=5,
                width=48,
                height=32,
                versions=("v1",),
                max_new_frames=1,
            )
            v1 = second["versions"]["v1"]
            self.assertEqual(v1["session_start_frame"], 2)
            self.assertEqual(v1["committed_frames_skipped_this_run"], [0, 1])
            self.assertEqual(v1["rendered_frames_this_run"], [2])
            self.assertEqual(v1["next_frame"], 3)
            after = read_json(checkpoint_path)
            self.assertEqual(after["committed_frames"][0]["json_sha256"], committed_hash)
            self.assertNotIn("orphan", orphan.read_text(encoding="utf-8"))
            self.assertEqual(after["committed_frames"][2]["index"], 2)
            self.assertEqual(after["resume_count"], 1)

    def test_replay_writes_byte_identical_frame_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            render_all(
                root,
                seed=123,
                frame_count=1,
                width=40,
                height=28,
                versions=("v1",),
            )
            frame_dir = root / "frames" / "v1"
            names = ("frame_0000.json", "frame_0000.npz", "frame_0000.png", "checkpoint.json")
            first = {name: (frame_dir / name).read_bytes() for name in names}
            render_all(
                root,
                seed=123,
                frame_count=1,
                width=40,
                height=28,
                versions=("v1",),
                reset_version=True,
            )
            second = {name: (frame_dir / name).read_bytes() for name in names}
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
