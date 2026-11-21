import unittest

from asr_gov.drift import adjudicate, roundtrip_drift_check
from asr_gov.errors import SemanticDrift
from asr_gov.formats import load_slices, dump_slices
from asr_gov.migrate import migrate
from asr_gov.sample_data import build_ground_truth


class TestMigrateDrift(unittest.TestCase):
    def setUp(self):
        self.gt = build_ground_truth()

    def test_upgrade_completions_recorded(self):
        v1_set = migrate(self.gt, 1).slice_set
        result = migrate(v1_set, 3)
        self.assertGreater(len(result.completions), 0)
        sources = {c["source"] for c in result.completions}
        self.assertIn("derived:text_whitespace", sources)
        self.assertIn("default:spk_unknown", sources)
        self.assertIn("default:[0.0,1.0]", sources)
        self.assertIn("interpolated:even_split", sources)
        fields = {c["field"] for c in result.completions}
        self.assertIn("token_timings", fields)
        self.assertIn("confidence_interval", fields)

    def test_downgrade_discards_recorded(self):
        result = migrate(self.gt, 1)
        self.assertGreater(len(result.discards), 0)
        rules = {d["rule"] for d in result.discards}
        self.assertIn("v_target_cannot_represent:token_timing", rules)
        self.assertIn("v_target_cannot_represent:confidence_interval", rules)
        fields = {d["field"] for d in result.discards}
        self.assertIn("speaker", fields)
        self.assertIn("segment_label", fields)
        self.assertIn("revision", fields)

    def test_no_silent_rewrite(self):
        result = migrate(self.gt, 1)
        for s in result.slice_set.slices:
            self.assertIsNone(s.speaker)
            self.assertIsNone(s.conf_lo)
            self.assertEqual(s.tokens, [])
        discarded_utts = {d["utterance_id"] for d in result.discards}
        self.assertEqual(discarded_utts, {"u1", "u2", "u3"})

    def test_adjudication_equal_across_versions(self):
        sets = {}
        for version in (1, 2, 3):
            projected = migrate(self.gt, version).slice_set
            text = dump_slices(projected, version)
            sets[version] = load_slices(text, self.gt.clip_id)
        report = adjudicate(sets)
        self.assertTrue(report.equal, report.mismatches)
        self.assertEqual(report.slice_ids, ["u1", "u2", "u3"])

    def test_adjudication_detects_mismatch(self):
        sets = {}
        for version in (1, 2, 3):
            projected = migrate(self.gt, version).slice_set
            sets[version] = load_slices(dump_slices(projected, version), "c")
        sets[2].slices[0].text = "tampered text"
        report = adjudicate(sets)
        self.assertFalse(report.equal)
        self.assertTrue(any(m.get("utterance_id") == "u1" for m in report.mismatches))

    def test_semantic_drift_via_v1(self):
        with self.assertRaises(SemanticDrift) as ctx:
            roundtrip_drift_check(self.gt, via_version=1)
        exc = ctx.exception
        self.assertEqual(exc.code, "E_SEMANTIC_DRIFT")
        affected = exc.details["affected_slices"]
        self.assertEqual(affected, ["u1", "u2", "u3"])
        fields = {u["field"] for u in exc.details["unrecoverable"]}
        self.assertIn("token_timings", fields)
        self.assertIn("confidence_interval", fields)

    def test_no_drift_for_v1_native(self):
        v1_set = migrate(self.gt, 1).slice_set
        restored = roundtrip_drift_check(v1_set, via_version=1)
        self.assertEqual(restored.slice_ids(), v1_set.slice_ids())

    def test_sub_ms_drift_detected(self):
        gt = build_ground_truth()
        gt.slices[0].start_us += 500
        with self.assertRaises(SemanticDrift) as ctx:
            roundtrip_drift_check(gt, via_version=1)
        fields = {u["field"] for u in ctx.exception.details["unrecoverable"]}
        self.assertIn("sub_ms_timing", fields)


if __name__ == "__main__":
    unittest.main()
