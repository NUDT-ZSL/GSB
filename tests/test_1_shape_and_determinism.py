import unittest

import numpy as np

from kfdiff.clipmodel import parse_task
from kfdiff.diffusion import generate_all_keyframes
from kfdiff.errors import KFError
from tests.helpers import make_studio


class ShapeValidationTests(unittest.TestCase):
    def test_illegal_interval_returns_e_clip_shape(self):
        bad_rows = [
            {"clip_id": "c", "total_frames": 10, "keyframe_interval": 4,
             "frame_shape": [8, 8, 3], "noise_seed": 1, "prompt": "p",
             "diffusion_steps": 4, "block_grid": 2, "revision": 1},
            {"clip_id": "c", "total_frames": 10, "keyframe_interval": 0,
             "frame_shape": [8, 8, 3], "noise_seed": 1, "prompt": "p",
             "diffusion_steps": 4, "block_grid": 2, "revision": 1},
            {"clip_id": "c", "total_frames": 10, "keyframe_interval": 3,
             "frame_shape": [8, 8], "noise_seed": 1, "prompt": "p",
             "diffusion_steps": 4, "block_grid": 2, "revision": 1},
        ]
        for row in bad_rows:
            with self.assertRaises(KFError) as ctx:
                parse_task(row)
            self.assertEqual(ctx.exception.code, "E_CLIP_SHAPE")
            self.assertTrue(ctx.exception.details["violations"])

    def test_valid_keyframe_index_relationship(self):
        studio, _ = make_studio()
        task = studio.tasks["clip_0001"]
        indices = task.keyframe_indices()
        self.assertEqual(indices[0], 0)
        self.assertEqual(indices[-1], task.total_frames - 1)
        self.assertTrue(all((b - a) == task.keyframe_interval
                            for a, b in zip(indices, indices[1:])))


class DeterminismTests(unittest.TestCase):
    def test_same_inputs_byte_identical_across_instances(self):
        studio_a, root = make_studio()
        from kfdiff.engine import Studio

        studio_b = Studio(root)
        for clip_id in ("clip_0001", "clip_0004", "clip_0008"):
            task = studio_a.tasks[clip_id]
            bundle_a = generate_all_keyframes(task, studio_a.config)
            bundle_b = generate_all_keyframes(studio_b.tasks[clip_id], studio_b.config)
            self.assertEqual(len(bundle_a["keyframes"]), task.keyframe_count)
            for kf_a, kf_b in zip(bundle_a["keyframes"], bundle_b["keyframes"]):
                self.assertTrue(np.array_equal(kf_a["pixels"], kf_b["pixels"]))
                self.assertEqual(kf_a["provenance"]["pixels_hash"],
                                 kf_b["provenance"]["pixels_hash"])

    def test_different_seed_changes_pixels(self):
        studio, _ = make_studio()
        task = studio.tasks["clip_0001"]
        bundle_a = generate_all_keyframes(task, studio.config)
        row = studio.task_rows["clip_0001"]
        row["noise_seed"] = row["noise_seed"] + 1
        changed = parse_task(row)
        bundle_b = generate_all_keyframes(changed, studio.config)
        self.assertFalse(np.array_equal(bundle_a["keyframes"][0]["pixels"],
                                        bundle_b["keyframes"][0]["pixels"]))


if __name__ == "__main__":
    unittest.main()
