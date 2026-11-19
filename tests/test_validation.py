import json
import os
import tempfile
import unittest

from voice_dialogue.config import ComponentConfig
from voice_dialogue.dataio import load_manifest
from voice_dialogue.errors import BusinessError, ErrorCode
from voice_dialogue.pipeline import run_pipeline
from voice_dialogue.synth import (
    TurnSpec,
    generate_dialog,
    generate_sample_bundle,
)


class ConfigValidationTests(unittest.TestCase):
    def _expect(self, kwargs, field=None):
        with self.assertRaises(BusinessError) as ctx:
            ComponentConfig.from_dict(kwargs)
        self.assertEqual(ctx.exception.code, ErrorCode.E_CONFIG_INVALID)
        if field:
            self.assertEqual(ctx.exception.field, field)

    def test_illegal_sample_rate(self):
        self._expect({"sample_rate": 8000}, "sample_rate")

    def test_illegal_frame_ms(self):
        self._expect({"frame_ms": 1.0}, "frame_ms")

    def test_hop_exceeds_frame(self):
        self._expect({"frame_ms": 20.0, "hop_ms": 40.0}, "hop_ms")

    def test_non_positive_max_speakers(self):
        self._expect({"max_speakers": 0}, "max_speakers")

    def test_unknown_config_field_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            ComponentConfig.from_dict({"max_speakers": 2, "bogus": 1})
        self.assertEqual(ctx.exception.code, ErrorCode.E_CONFIG_INVALID)


class ManifestValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmp, "samples")
        self.manifest = generate_sample_bundle(self.data_dir)

    def test_duplicate_dialog_id(self):
        with open(self.manifest, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh]
        rows[1]["dialog_id"] = rows[0]["dialog_id"]
        dup = os.path.join(self.data_dir, "dup.jsonl")
        with open(dup, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        with self.assertRaises(BusinessError) as ctx:
            load_manifest(dup)
        self.assertEqual(ctx.exception.code, ErrorCode.E_DUPLICATE_DIALOG)

    def test_wrong_sample_rate_rejected(self):
        with open(self.manifest, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh]
        rows[0]["sample_rate"] = 44100
        bad = os.path.join(self.data_dir, "sr.jsonl")
        with open(bad, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        with self.assertRaises(BusinessError) as ctx:
            run_pipeline(bad, ComponentConfig())
        self.assertEqual(ctx.exception.code, ErrorCode.E_CONFIG_INVALID)
        self.assertEqual(ctx.exception.field, "sample_rate")

    def test_timeline_out_of_bounds(self):
        with self.assertRaises(BusinessError) as ctx:
            generate_dialog(
                "bad",
                [TurnSpec(0, 100.0, 99999.0, 0.0, 0.0)],
                1000.0, 1, self.tmp,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.E_CONFIG_INVALID)

    def test_illegal_gain(self):
        with self.assertRaises(BusinessError) as ctx:
            generate_dialog(
                "badg",
                [TurnSpec(0, 100.0, 500.0, 0.0, 0.0, gain=3.0)],
                1000.0, 1, self.tmp,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.E_CONFIG_INVALID)


if __name__ == "__main__":
    unittest.main()
