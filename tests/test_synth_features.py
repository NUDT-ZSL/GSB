import os
import tempfile
import unittest

import numpy as np

from voice_dialogue.config import ComponentConfig
from voice_dialogue.dataio import load_ground_truth, load_pcm
from voice_dialogue.features import extract_features
from voice_dialogue.framing import frame_count, ms_to_samples
from voice_dialogue.synth import TurnSpec, generate_dialog


class FramingTests(unittest.TestCase):
    def test_sample_accurate_boundaries(self):
        sr = 16000
        size = ms_to_samples(20.0, sr)
        hop = ms_to_samples(20.0, sr)
        self.assertEqual(size, 320)
        self.assertEqual(frame_count(3200, size, hop), 10)
        self.assertEqual(frame_count(3199, size, hop), 9)

    def test_hop_must_not_exceed_frame(self):
        from voice_dialogue.errors import BusinessError, ErrorCode
        with self.assertRaises(BusinessError) as ctx:
            ComponentConfig(frame_ms=20.0, hop_ms=30.0).validate()
        self.assertEqual(ctx.exception.code, ErrorCode.E_CONFIG_INVALID)


class DeterminismTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        turns = [
            TurnSpec(0, 200.0, 1500.0, 0.4, 0.6),
            TurnSpec(1, 1700.0, 3000.0, -0.3, 0.2),
            TurnSpec(0, 3200.0, 4500.0, 0.1, -0.5),
        ]
        self.row_a = generate_dialog("d_a", turns, 5000.0, 42, self.tmp)
        self.row_b = generate_dialog("d_a", turns, 5000.0, 42, self.tmp)

    def test_pcm_and_gt_byte_identical(self):
        pcm_path = os.path.join(self.tmp, "d_a.pcm")
        gt_path = os.path.join(self.tmp, "d_a.gt.csv")
        with open(pcm_path, "rb") as fh:
            pcm_a = fh.read()
        with open(pcm_path, "rb") as fh:
            pcm_b = fh.read()
        self.assertEqual(pcm_a, pcm_b)
        with open(gt_path, "rb") as fh:
            gt_a = fh.read()
        generate_dialog(
            "d_a",
            [TurnSpec(0, 200.0, 1500.0, 0.4, 0.6),
             TurnSpec(1, 1700.0, 3000.0, -0.3, 0.2),
             TurnSpec(0, 3200.0, 4500.0, 0.1, -0.5)],
            5000.0, 42, self.tmp,
        )
        with open(gt_path, "rb") as fh:
            gt_b = fh.read()
        self.assertEqual(gt_a, gt_b)

    def test_feature_frame_count_matches_gt(self):
        config = ComponentConfig()
        pcm = load_pcm(os.path.join(self.tmp, self.row_a["pcm_path"]), "d_a")
        gt = load_ground_truth(
            os.path.join(self.tmp, self.row_a["gt_path"]), "d_a"
        )
        result = extract_features(
            pcm, config.sample_rate, config.frame_ms, config.hop_ms,
            dim=8, expected_frames=gt.shape[0],
        )
        self.assertEqual(result.features.shape[0], gt.shape[0])
        again = extract_features(
            pcm, config.sample_rate, config.frame_ms, config.hop_ms,
            dim=8, expected_frames=gt.shape[0],
        )
        np.testing.assert_array_equal(result.features, again.features)

    def test_frame_mismatch_code(self):
        from voice_dialogue.errors import BusinessError, ErrorCode
        config = ComponentConfig()
        pcm = load_pcm(os.path.join(self.tmp, self.row_a["pcm_path"]), "d_a")
        with self.assertRaises(BusinessError) as ctx:
            extract_features(
                pcm, config.sample_rate, config.frame_ms, config.hop_ms,
                dim=8, expected_frames=999, dialog_id="d_a",
            )
        self.assertEqual(ctx.exception.code, ErrorCode.E_FRAME_MISMATCH)
        self.assertEqual(ctx.exception.dialog_id, "d_a")

    def test_strided_reconstruction_keeps_full_frames(self):
        config = ComponentConfig()
        pcm = load_pcm(os.path.join(self.tmp, self.row_a["pcm_path"]), "d_a")
        gt = load_ground_truth(
            os.path.join(self.tmp, self.row_a["gt_path"]), "d_a"
        )
        result = extract_features(
            pcm, config.sample_rate, config.frame_ms, config.hop_ms,
            dim=4, expected_frames=gt.shape[0], frame_stride=4,
        )
        self.assertEqual(result.features.shape[0], gt.shape[0])
        self.assertEqual(result.frame_stride, 4)


if __name__ == "__main__":
    unittest.main()
