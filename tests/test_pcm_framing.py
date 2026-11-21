import unittest

from asr_gov.pcm import synthesize_clip, speech_intervals_ms
from asr_gov.framing import frame_pcm, detect_boundaries
from asr_gov.sample_data import default_config


class TestPcmFraming(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()

    def test_pcm_deterministic_bytes(self):
        a = synthesize_clip()
        b = synthesize_clip()
        self.assertEqual(a.to_bytes(), b.to_bytes())
        self.assertEqual(a.digest(), b.digest())

    def test_frame_boundaries_exact(self):
        clip = synthesize_clip()
        feats = frame_pcm(clip.samples, 16000, 25, 10)
        self.assertEqual(feats.frame_length, 400)
        self.assertEqual(feats.frame_shift, 160)
        expected = 1 + (clip.n_samples - 400) // 160
        self.assertEqual(feats.n_frames, expected)
        self.assertEqual(int(feats.starts[1]) - int(feats.starts[0]), 160)

    def test_features_deterministic(self):
        clip = synthesize_clip()
        f1 = frame_pcm(clip.samples, 16000, 25, 10)
        f2 = frame_pcm(clip.samples, 16000, 25, 10)
        self.assertEqual(f1.digest(), f2.digest())

    def test_candidates_cover_speech_segments(self):
        clip = synthesize_clip()
        feats = frame_pcm(clip.samples, 16000, 25, 10)
        cands = detect_boundaries(feats, self.cfg.energy_threshold_high_mult,
                                  self.cfg.energy_threshold_low_mult,
                                  self.cfg.min_slice_ms, self.cfg.max_slices)
        intervals = speech_intervals_ms()
        self.assertEqual(len(cands), len(intervals))
        for cand, (start_ms, end_ms) in zip(cands, intervals):
            self.assertLessEqual(abs(cand.start_ms - start_ms), 40)
            self.assertLessEqual(abs(cand.end_ms - end_ms), 40)

    def test_max_slices_tie_break_by_start_sample(self):
        clip = synthesize_clip()
        feats = frame_pcm(clip.samples, 16000, 25, 10)
        cands = detect_boundaries(feats, 6.0, 2.0, 200, max_slices=2)
        self.assertEqual(len(cands), 2)
        starts = [c.start_sample for c in cands]
        self.assertEqual(starts, sorted(starts))


if __name__ == "__main__":
    unittest.main()
