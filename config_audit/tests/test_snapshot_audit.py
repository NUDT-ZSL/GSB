import json
import tempfile
import unittest
from pathlib import Path

from config_audit.audit import run_audit
from config_audit.errors import RangeError, ReplayError
from config_audit.generate_samples import write_samples
from config_audit.snapshot import (
    restore_and_replay,
    save_snapshot,
    current_archive_versions,
)


class SnapshotAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.samples = self.root / "samples"
        write_samples(self.samples)
        self.inputs = [self.samples / "node_a.json",
                       self.samples / "node_b.json"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_repeated_audit_bit_identical(self):
        r1 = run_audit(self.inputs, places=2, abs_tolerance="5",
                       relative_ppm=1000)
        r2 = run_audit(self.inputs, places=2, abs_tolerance="5",
                       relative_ppm=1000)
        self.assertEqual(r1["canonical_hash"], r2["canonical_hash"])

    def test_snapshot_save_restore_replay_identical(self):
        report = run_audit(self.inputs, places=2, abs_tolerance="5")
        snap = self.root / "snap.json"
        save_snapshot(report, current_archive_versions(self.inputs), snap)
        restored = restore_and_replay(self.inputs, snap)
        self.assertEqual(restored["canonical_hash"],
                         report["canonical_hash"])
        self.assertEqual(restored["restore"]["action"],
                         "replayed_same_versions")

    def test_version_mismatch_whole_replacement(self):
        report = run_audit(self.inputs, places=2, abs_tolerance="5")
        snap = self.root / "snap.json"
        save_snapshot(report, current_archive_versions(self.inputs), snap)
        v2 = [self.samples / "node_a_v2.json",
              self.samples / "node_b.json"]
        restored = restore_and_replay(v2, snap)
        self.assertEqual(restored["restore"]["action"],
                         "whole_state_replaced_version_mismatch")

    def test_replay_mismatch_raises_e_replay(self):
        report = run_audit(self.inputs, places=2, abs_tolerance="5")
        snap = self.root / "snap.json"
        save_snapshot(report, current_archive_versions(self.inputs), snap)
        doc = json.loads(snap.read_text(encoding="utf-8"))
        # Tamper a stored verdict while keeping versions identical.
        first_key = next(iter(doc["report"]["keys"]))
        doc["report"]["keys"][first_key]["key_verdict"] = "deviating"
        snap.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        with self.assertRaises(ReplayError) as ctx:
            restore_and_replay(self.inputs, snap)
        self.assertEqual(ctx.exception.code, "E_REPLAY")
        self.assertTrue(any(first_key in loc for loc in
                            ctx.exception.locations))

    def test_range_error_after_quantization(self):
        bad = [self.samples / "node_range_bad.json"]
        with self.assertRaises(RangeError) as ctx:
            run_audit(bad, places=2)
        self.assertEqual(ctx.exception.code, "E_RANGE")
        self.assertEqual(ctx.exception.key, "sampling.rate")

    def test_grid_change_marks_stale(self):
        coarse = run_audit(self.inputs, places=0, abs_tolerance="5")
        fine = run_audit(self.inputs, places=3, abs_tolerance="5",
                         previous_grid_sig=coarse["grid"]["signature"])
        self.assertTrue(fine["grid"]["grid_changed"])
        self.assertEqual(fine["grid"]["stale_previous_signature"],
                         coarse["grid"]["signature"])
        # The two grids must never share a conclusion set.
        self.assertNotEqual(coarse["canonical_hash"],
                            fine["canonical_hash"])


if __name__ == "__main__":
    unittest.main()
