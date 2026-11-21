import copy
import dataclasses
import unittest

from asr_gov.dag import Pipeline
from asr_gov.errors import LineageStale
from asr_gov.pcm import synthesize_clip
from asr_gov.sample_data import build_ground_truth, build_vocabs, default_config


class TestDag(unittest.TestCase):
    def setUp(self):
        self.clip = synthesize_clip()
        self.cfg = default_config()
        self.vocab1, self.vocab2 = build_vocabs()
        self.gt = build_ground_truth()
        self.pipe = Pipeline()
        self.r1 = self.pipe.run(self.clip, self.gt, self.cfg, self.vocab1, 1)

    def test_initial_run_all_misses(self):
        self.assertEqual(self.r1.cache_hits, [])
        self.assertEqual(len(self.r1.slices), 3)

    def test_rerun_fully_cached(self):
        r2 = self.pipe.run(self.clip, self.gt, self.cfg, self.vocab1, 1)
        self.assertEqual(r2.cache_misses, [])
        self.assertEqual(r2.affected_slices, [])
        self.assertEqual(r2.timeline(), self.r1.timeline())

    def test_edit_slice_affects_only_that_slice(self):
        gt2 = copy.deepcopy(self.gt)
        gt2.get("u2").text = "good evening friends"
        gt2.get("u2").tokens = []
        gt2.get("u2").flat_tokens = None
        r2 = self.pipe.run(self.clip, gt2, self.cfg, self.vocab1, 2)
        self.assertEqual(r2.affected_slices, ["u2"])
        self.assertTrue(any(k.startswith("framing:") for k in r2.cache_hits))
        self.assertEqual(r2.timeline(), self.r1.timeline())

    def test_vocab_bump_affects_only_involved_slices(self):
        r2 = self.pipe.run(self.clip, self.gt, self.cfg, self.vocab2, 1)
        self.assertEqual(r2.affected_slices, ["u1"])
        self.assertEqual(r2.timeline(), self.r1.timeline())

    def test_frame_shift_change_affects_all(self):
        cfg2 = dataclasses.replace(self.cfg, version=2, frame_shift_ms=20)
        r2 = self.pipe.run(self.clip, self.gt, cfg2, self.vocab1, 1)
        self.assertEqual(r2.affected_slices, ["u1", "u2", "u3"])

    def test_threshold_change_keeps_timeline_and_slices(self):
        cfg2 = dataclasses.replace(self.cfg, version=2,
                                   energy_threshold_high_mult=8.0)
        r2 = self.pipe.run(self.clip, self.gt, cfg2, self.vocab1, 1)
        self.assertEqual(r2.affected_slices, [])
        self.assertTrue(any(k.startswith("boundary:") for k in r2.cache_misses))
        self.assertEqual(r2.timeline(), self.r1.timeline())

    def test_lineage_stale_rejected(self):
        cfg2 = dataclasses.replace(self.cfg, version=2, frame_shift_ms=20)
        r2 = self.pipe.run(self.clip, self.gt, cfg2, self.vocab1, 1)
        with self.assertRaises(LineageStale) as ctx:
            self.pipe.verify_lineage(self.r1.slices[0].lineage,
                                     r2.slices[0].lineage.cache_keys)
        stale_nodes = [d["node"] for d in ctx.exception.details["stale_dependencies"]]
        self.assertIn("framing", stale_nodes)
        self.assertIn("validate", stale_nodes)

    def test_lineage_replay_consistent(self):
        record = self.r1.slices[0].lineage
        artifacts = self.pipe.replay(record)
        self.assertEqual(set(artifacts),
                         {"framing", "boundary", "normalize", "validate", "snapshot"})
        self.assertEqual(artifacts["normalize"]["utterance_id"], "u1")

    def test_replay_missing_cache_rejected(self):
        pipe2 = Pipeline()
        with self.assertRaises(LineageStale):
            pipe2.replay(self.r1.slices[0].lineage)

    def test_lineage_records_complete(self):
        for s in self.r1.slices:
            self.assertEqual(s.lineage.node_path,
                             ["framing", "boundary", "normalize", "validate", "snapshot"])
            self.assertEqual(set(s.lineage.cache_keys), set(s.lineage.node_path))
            self.assertIn("config_version", s.lineage.input_versions)


if __name__ == "__main__":
    unittest.main()
