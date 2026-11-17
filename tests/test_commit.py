import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mocap_audio import commit as commit_mod
from mocap_audio.commit import commit_outputs
from mocap_audio.errors import WriteCommitError
from mocap_audio.wavio import read_header


class CommitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wav = os.path.join(self.tmp, "p.wav")
        self.report = os.path.join(self.tmp, "r.json")
        self.samples = np.full((100, 2), 0.25)

    def test_version_unchanged_keeps_old_files(self):
        commit_outputs(self.wav, self.report, self.samples, 48000, 16,
                       '{"snapshot_id": "v1"}', "v1")
        before = os.path.getmtime(self.wav)
        result = commit_outputs(self.wav, self.report, self.samples, 48000, 16,
                                '{"snapshot_id": "v1"}', "v1")
        self.assertFalse(result["replaced"])
        self.assertEqual(os.path.getmtime(self.wav), before)

    def test_version_changed_replaces_both(self):
        commit_outputs(self.wav, self.report, self.samples, 48000, 16,
                       '{"snapshot_id": "v1"}', "v1")
        result = commit_outputs(self.wav, self.report, self.samples, 44100, 24,
                                '{"snapshot_id": "v2"}', "v2")
        self.assertTrue(result["replaced"])
        self.assertEqual(read_header(self.wav).sample_rate, 44100)
        self.assertEqual(json.load(open(self.report, encoding="utf-8"))
                         ["snapshot_id"], "v2")

    def test_failure_rolls_back_old_version(self):
        commit_outputs(self.wav, self.report, self.samples, 48000, 16,
                       '{"snapshot_id": "v1"}', "v1")
        old_header = read_header(self.wav)
        real_replace = os.replace
        calls = {"count": 0}

        def flaky(src, dst):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated report replace failure")
            return real_replace(src, dst)

        with mock.patch.object(commit_mod.os, "replace", side_effect=flaky):
            with self.assertRaises(WriteCommitError) as ctx:
                commit_outputs(self.wav, self.report, self.samples, 44100, 24,
                               '{"snapshot_id": "v2"}', "v2")
        self.assertEqual(ctx.exception.code, "E_WR")
        self.assertEqual(read_header(self.wav).sample_rate,
                         old_header.sample_rate)
        self.assertEqual(json.load(open(self.report, encoding="utf-8"))
                         ["snapshot_id"], "v1")
        self.assertFalse(any(name.endswith(".tmp.wav") or
                             name.endswith(".tmp.json")
                             for name in os.listdir(self.tmp)))

if __name__ == "__main__":
    unittest.main()

