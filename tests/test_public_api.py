import os
import tempfile
import unittest

from voice_dialogue import (
    ComponentConfig,
    decide_path,
    extract_features,
    observe_scale,
)
from voice_dialogue.errors import BusinessError, ErrorCode
from voice_dialogue.synth import TurnSpec, generate_dialog
from voice_dialogue.dataio import load_ground_truth, load_pcm


class PublicEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.row = generate_dialog(
            "pub",
            [TurnSpec(0, 200.0, 2000.0, 0.3, 0.6),
             TurnSpec(1, 2200.0, 4000.0, -0.2, 0.1)],
            4500.0, 7, self.tmp,
        )

    def test_feature_entry_both_dims(self):
        config = ComponentConfig()
        pcm = load_pcm(os.path.join(self.tmp, self.row["pcm_path"]), "pub")
        gt = load_ground_truth(os.path.join(self.tmp, self.row["gt_path"]), "pub")
        fine = extract_features(
            pcm, config.sample_rate, config.frame_ms, config.hop_ms,
            dim=8, expected_frames=gt.shape[0],
        )
        approx = extract_features(
            pcm, config.sample_rate, config.frame_ms, config.hop_ms,
            dim=4, expected_frames=gt.shape[0], frame_stride=2,
        )
        self.assertEqual(fine.features.shape, (gt.shape[0], 8))
        self.assertEqual(approx.features.shape, (gt.shape[0], 4))
        self.assertEqual(approx.frame_stride, 2)

    def test_decision_entry_deterministic_and_serializable(self):
        import json
        config = ComponentConfig()
        rows = [
            {"num_frames": 100, "speaker_ids": [0, 1]},
            {"num_frames": 200, "speaker_ids": [0, 1, 2]},
        ]
        obs = observe_scale(rows, config)
        d1 = decide_path(obs, config).to_dict()
        d2 = decide_path(obs, config).to_dict()
        self.assertEqual(d1, d2)
        json.dumps(d1, sort_keys=True)
        self.assertIn(d1["path"], ("fine", "approximate"))

    def test_bad_dim_rejected(self):
        config = ComponentConfig()
        pcm = load_pcm(os.path.join(self.tmp, self.row["pcm_path"]), "pub")
        with self.assertRaises(BusinessError) as ctx:
            extract_features(
                pcm, config.sample_rate, config.frame_ms, config.hop_ms,
                dim=3, expected_frames=10,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.E_CONFIG_INVALID)


if __name__ == "__main__":
    unittest.main()
