import copy
import dataclasses
import unittest

from asr_gov.errors import AudioRange, EmptySlice, NoActivity, ResourceLimit
from asr_gov.framing import frame_pcm
from asr_gov.pcm import synthesize_clip
from asr_gov.sample_data import build_ground_truth, default_config
from asr_gov.validate import validate_slices


class TestValidate(unittest.TestCase):
    def setUp(self):
        self.clip = synthesize_clip()
        self.cfg = default_config()
        self.features = frame_pcm(self.clip.samples, 16000, 25, 10)
        self.gt = build_ground_truth()

    def _validate(self, slices, cfg=None, **kwargs):
        cfg = cfg or self.cfg
        return validate_slices(slices, self.features, self.clip.n_samples,
                               cfg.energy_threshold_low_mult, cfg,
                               clip_id=self.clip.clip_id, **kwargs)

    def test_ground_truth_passes(self):
        report = self._validate(self.gt.slices)
        self.assertEqual(report.checked, 3)
        self.assertEqual(report.degradations, [])

    def test_audio_range(self):
        bad = copy.deepcopy(self.gt)
        bad.get("u3").end_us = 999_999_000
        with self.assertRaises(AudioRange) as ctx:
            self._validate(bad.slices)
        self.assertIn("u3", ctx.exception.location)

    def test_empty_slice(self):
        bad = copy.deepcopy(self.gt)
        bad.get("u1").end_us = bad.get("u1").start_us
        with self.assertRaises(EmptySlice):
            self._validate(bad.slices)

    def test_no_activity(self):
        bad = copy.deepcopy(self.gt)
        bad.get("u1").start_us = 1_250_000
        bad.get("u1").end_us = 1_550_000
        with self.assertRaises(NoActivity) as ctx:
            self._validate(bad.slices)
        self.assertIn("u1", ctx.exception.location)

    def test_slice_count_limit(self):
        tight = dataclasses.replace(self.cfg, max_slices_per_clip=2)
        with self.assertRaises(ResourceLimit) as ctx:
            self._validate(self.gt.slices, cfg=tight)
        self.assertEqual(ctx.exception.details["limit"], "max_slices_per_clip")

    def test_memory_limit_hard(self):
        tiny = dataclasses.replace(self.cfg, max_resident_feature_bytes=1024)
        with self.assertRaises(ResourceLimit):
            self._validate(self.gt.slices, cfg=tiny, allow_degrade=False)

    def test_memory_degrade_keeps_verdicts(self):
        tiny = dataclasses.replace(self.cfg, max_resident_feature_bytes=1024,
                                   max_recompute_chunks=2)
        report = self._validate(self.gt.slices, cfg=tiny, requested_chunks=6)
        categories = {d["category"] for d in report.degradations}
        self.assertIn("resident_feature_downsample", categories)
        self.assertIn("chunk_merge", categories)
        self.assertEqual(report.verdicts, {"u1": "ok", "u2": "ok", "u3": "ok"})
        self.assertEqual(report.recompute_chunks, 2)


if __name__ == "__main__":
    unittest.main()
