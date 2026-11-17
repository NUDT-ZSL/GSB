import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from h2workbench.errors import DiagError, E_REPLAY
from h2workbench.session import Session
from h2workbench.snapshot import (list_snapshots, load_snapshot,
                                  restore_into_session, save_snapshot,
                                  verify_replay)

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.session = Session.from_file(
            os.path.join(SAMPLES, "manifest_medium.json"))
        self.session.run()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmpdir.name, "snap.json")
        save_snapshot(self.session, self.path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_roundtrip_and_replay_ok(self):
        snap = load_snapshot(self.path)
        result = verify_replay(snap)
        self.assertEqual(result["status"], "ok")
        self.assertGreater(result["frames_replayed"], 0)

    def test_version_mismatch_whole_replace(self):
        snap = load_snapshot(self.path)
        old_version = self.session.manifest["version"]
        self.session.manifest = dict(self.session.manifest,
                                     version=old_version + 1)
        result = restore_into_session(self.session, snap)
        self.assertTrue(result["replaced"])
        self.assertEqual(self.session.manifest["version"], old_version)
        self.assertEqual(result["previous_manifest_version"],
                         old_version + 1)
        self.assertEqual(result["replay"]["status"], "ok")

    def test_same_version_no_replace(self):
        snap = load_snapshot(self.path)
        result = restore_into_session(self.session, snap)
        self.assertFalse(result["replaced"])

    def test_tampered_plan_rejected(self):
        snap = load_snapshot(self.path)
        snap["plan"]["frames"][2]["bytes"] += 1
        with self.assertRaises(DiagError) as ctx:
            verify_replay(snap)
        self.assertEqual(ctx.exception.code, E_REPLAY)
        self.assertIn("frame_index", ctx.exception.details)

    def test_tampered_grants_rejected(self):
        snap = load_snapshot(self.path)
        snap["plan"]["predicted_grants"][0][1] += 1
        with self.assertRaises(DiagError) as ctx:
            verify_replay(snap)
        self.assertEqual(ctx.exception.code, E_REPLAY)

    def test_listing_is_sorted(self):
        save_snapshot(self.session,
                      os.path.join(self.tmpdir.name, "b.json"))
        save_snapshot(self.session,
                      os.path.join(self.tmpdir.name, "a.json"))
        names = list_snapshots(self.tmpdir.name)
        self.assertEqual(names, sorted(names))


if __name__ == "__main__":
    unittest.main()
