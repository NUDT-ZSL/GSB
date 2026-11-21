import unittest

from asr_gov.errors import (FormatVersionUnsupported, FormatCorrupt,
                            TimeInvalid, DupUtterance)
from asr_gov.formats import load_slices, detect_format, dump_slices
from asr_gov.migrate import migrate
from asr_gov.sample_data import build_ground_truth


class TestFormats(unittest.TestCase):
    def test_detect_by_header(self):
        self.assertEqual(detect_format("#format: asr-slices-v1\nu1|0|1|x\n"), 1)
        self.assertEqual(detect_format("#format: asr-slices-v2\na=b\n"), 2)

    def test_detect_by_field_set(self):
        self.assertEqual(detect_format("u1|0|1|hello\n"), 1)
        self.assertEqual(detect_format("utterance_id=u1|start_us=0\n"), 2)
        self.assertEqual(detect_format('{"format_version": 3, "slices": []}'), 3)

    def test_unsupported_version(self):
        with self.assertRaises(FormatVersionUnsupported):
            detect_format("#format: asr-slices-v9\nu1|0|1|x\n")
        with self.assertRaises(FormatVersionUnsupported):
            detect_format('{"format_version": 4, "slices": []}')

    def test_v1_field_count(self):
        with self.assertRaises(FormatCorrupt) as ctx:
            load_slices("u1|0|100\n", "clip-x")
        self.assertEqual(ctx.exception.clip_id, "clip-x")
        self.assertIn("line 1", ctx.exception.location)

    def test_v1_time_not_int(self):
        with self.assertRaises(TimeInvalid):
            load_slices("u1|abc|100|hi\n", "c")

    def test_v1_start_ge_end(self):
        with self.assertRaises(TimeInvalid):
            load_slices("u1|100|100|hi\n", "c")

    def test_dup_utterance(self):
        with self.assertRaises(DupUtterance) as ctx:
            load_slices("u1|0|100|a\nu1|100|200|b\n", "clip-y")
        self.assertEqual(ctx.exception.clip_id, "clip-y")

    def test_v2_missing_key(self):
        with self.assertRaises(FormatCorrupt):
            load_slices("utterance_id=u1|start_us=0|end_us=5\n", "c")

    def test_v2_time_not_int(self):
        line = ("utterance_id=u1|start_us=x|end_us=5|text=t|tokens=a|"
                "speaker=s|confidence=0.5|vocab_version=v\n")
        with self.assertRaises(TimeInvalid):
            load_slices(line, "c")

    def test_v3_missing_key(self):
        doc = '{"format_version": 3, "slices": [{"utterance_id": "u1"}]}'
        with self.assertRaises(FormatCorrupt) as ctx:
            load_slices(doc, "c")
        self.assertIn("slices[0]", ctx.exception.location)

    def test_v3_time_not_int(self):
        gt = build_ground_truth()
        import json
        doc = json.loads(dump_slices(gt, 3))
        doc["slices"][0]["start_us"] = "not-an-int"
        with self.assertRaises(TimeInvalid):
            load_slices(json.dumps(doc), "c")

    def test_roundtrip_parse_dump(self):
        gt = build_ground_truth()
        for version in (1, 2, 3):
            projected = migrate(gt, version).slice_set
            text = dump_slices(projected, version)
            loaded = load_slices(text, gt.clip_id)
            self.assertEqual(loaded.slice_ids(), gt.slice_ids())
            for orig, back in zip(projected.slices, loaded.slices):
                self.assertEqual(orig.start_ms, back.start_ms)
                self.assertEqual(orig.end_ms, back.end_ms)
                self.assertEqual(orig.text, back.text)


if __name__ == "__main__":
    unittest.main()
