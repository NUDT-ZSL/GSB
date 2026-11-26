import unittest

import numpy as np

from kfdiff.diffusion import generate_keyframe
from kfdiff.errors import KFError
from tests.helpers import make_studio


class ConsistencyTests(unittest.TestCase):
    def test_block_and_whole_identical_for_all_clips(self):
        studio, _ = make_studio()
        for clip_id in sorted(studio.tasks):
            verdict = studio.adjudicate_consistency(clip_id, raise_on_mismatch=False)
            self.assertTrue(verdict["exact_match"], clip_id)
            self.assertTrue(verdict["pixel_verdict"]["exact_match"])
            self.assertTrue(verdict["boundary_verdict"]["exact_match"])

    def test_block_mismatch_reports_first_frame_and_pixel(self):
        studio, _ = make_studio()

        def faulty(sources, ordinal):
            produced = generate_keyframe(sources, ordinal)
            if ordinal == 2:
                produced["pixels"][5, 7, 1] = produced["pixels"][5, 7, 1] ^ np.uint8(0xFF)
            return produced

        with self.assertRaises(KFError) as ctx:
            studio.adjudicate_consistency("clip_0001", block_frame_generator=faulty)
        self.assertEqual(ctx.exception.code, "E_BLOCK_MISMATCH")
        location = ctx.exception.details["pixel_verdict"]["first_mismatch"]
        self.assertEqual(location["ordinal"], 2)
        self.assertEqual(location["pixel_coordinate_hwc"], [5, 7, 1])
        self.assertIsNotNone(location["frame_index"])

    def test_shape_mismatch_detected(self):
        studio, _ = make_studio()

        def faulty_shape(sources, ordinal):
            produced = generate_keyframe(sources, ordinal)
            if ordinal == 1:
                produced["pixels"] = produced["pixels"][:-1, :, :]
            return produced

        verdict = studio.adjudicate_consistency(
            "clip_0001", block_frame_generator=faulty_shape, raise_on_mismatch=False
        )
        self.assertFalse(verdict["exact_match"])
        self.assertEqual(verdict["pixel_verdict"]["first_mismatch"]["reason"], "shape_mismatch")


if __name__ == "__main__":
    unittest.main()
