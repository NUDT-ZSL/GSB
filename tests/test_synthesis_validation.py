import tempfile
import unittest
from pathlib import Path

from aed_scale.codes import BusinessCode
from aed_scale.errors import AedError
from aed_scale.io import load_manifest, load_truth_frames, sha256_file, write_jsonl
from aed_scale.synthesis import generate_dataset
from aed_scale.validation import validate_dialog_timeline


class SynthesisValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tempdir.name)
        cls.first = generate_dataset(
            cls.root / "first",
            seed=73,
            num_dialogs=2,
            duration_ms=1_400,
            speaker_count=3,
        )
        cls.second = generate_dataset(
            cls.root / "second",
            seed=73,
            num_dialogs=2,
            duration_ms=1_400,
            speaker_count=3,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def test_seed_reproduces_bytes(self):
        self.assertEqual(self.first["manifest_sha256"], self.second["manifest_sha256"])
        for relative in (
            "manifest.jsonl",
            "dialogs/dialog_000/audio.wav",
            "dialogs/dialog_000/frames.jsonl",
            "dialogs/dialog_001/audio.wav",
            "dialogs/dialog_001/frames.jsonl",
        ):
            self.assertEqual(
                sha256_file(self.root / "first" / relative),
                sha256_file(self.root / "second" / relative),
                relative,
            )

    def test_truth_schema_and_frame_count(self):
        manifest = load_manifest(self.root / "first" / "manifest.jsonl")
        self.assertEqual([row["dialog_id"] for row in manifest], ["dialog_000", "dialog_001"])
        for row in manifest:
            self.assertEqual(row["sample_rate"], 16_000)
            self.assertIn("modality_mask", row)
            truth = load_truth_frames(
                self.root / "first" / row["truth_path"],
                dialog_id=row["dialog_id"],
            )
            self.assertEqual(len(truth), row["duration_ms"] // 10)
            for expected_index, frame in enumerate(truth):
                self.assertEqual(frame["frame_index"], expected_index)
                self.assertIn(
                    frame["event_type"],
                    {"background", "cough", "keyboard", "knock", "laughter"},
                )
                self.assertIn("event_id", frame)
                self.assertIn("speaker_id", frame)
                self.assertIsInstance(frame["is_speech"], bool)

    def test_duplicate_dialog_manifest_is_rejected(self):
        manifest_path = self.root / "first" / "manifest.jsonl"
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
        duplicate_path = self.root / "duplicate_manifest.jsonl"
        duplicate_path.write_text(lines[0] + "\n" + lines[0] + "\n", encoding="utf-8")
        with self.assertRaises(AedError) as context:
            load_manifest(duplicate_path)
        self.assertEqual(context.exception.code, BusinessCode.E_DUP_DIALOG.value)
        self.assertEqual(context.exception.dialog_id, "dialog_000")

    def test_timeline_gain_and_bounds_errors(self):
        with self.assertRaises(AedError) as gain_context:
            validate_dialog_timeline(
                {"events": [{"start_ms": 10, "end_ms": 100, "gain": 9.0}]},
                dialog_id="dialog_gain",
                duration_ms=1_000,
            )
        self.assertEqual(gain_context.exception.code, BusinessCode.E_TIMELINE_INVALID.value)
        self.assertEqual(gain_context.exception.field, "events[0].gain")
        self.assertEqual(gain_context.exception.dialog_id, "dialog_gain")

        with self.assertRaises(AedError) as range_context:
            validate_dialog_timeline(
                {"turns": [{"start_ms": 900, "end_ms": 1_100}]},
                dialog_id="dialog_range",
                duration_ms=1_000,
            )
        self.assertEqual(range_context.exception.code, BusinessCode.E_TIMELINE_INVALID.value)
        self.assertEqual(range_context.exception.dialog_id, "dialog_range")

    def test_timeline_event_type_is_controlled(self):
        with self.assertRaises(AedError) as context:
            validate_dialog_timeline(
                {
                    "events": [
                        {
                            "event_type": "siren",
                            "start_ms": 10,
                            "end_ms": 100,
                            "gain": 0.5,
                        }
                    ]
                },
                dialog_id="dialog_type",
                duration_ms=1_000,
            )
        self.assertEqual(context.exception.code, BusinessCode.E_EVENT_TYPE.value)
        self.assertEqual(context.exception.dialog_id, "dialog_type")


if __name__ == "__main__":
    unittest.main()
