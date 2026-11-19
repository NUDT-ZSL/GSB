import json
import os
import tempfile
import unittest

from voice_dialogue.config import ComponentConfig
from voice_dialogue.errors import BusinessError, ErrorCode
from voice_dialogue.pipeline import run_pipeline
from voice_dialogue.snapshot import (
    SNAPSHOT_FILE,
    build_snapshot,
    load_snapshot,
    save_snapshot,
)
from voice_dialogue.synth import generate_sample_bundle


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmp, "samples")
        self.manifest = generate_sample_bundle(
            self.data_dir, small_dialogs=1, large_dialogs=1
        )
        with open(self.manifest, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if "small" in l]
        self.small = os.path.join(self.data_dir, "small.jsonl")
        with open(self.small, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")

    def test_snapshot_round_trip_and_replacement(self):
        snap_dir = os.path.join(self.tmp, "snap")
        run_pipeline(
            self.small, ComponentConfig(), snapshot_dir=snap_dir
        )
        loaded = load_snapshot(snap_dir)
        self.assertEqual(loaded["version"], 1)

        snap2 = build_snapshot(2, {"k": 1}, {"p": "x"}, {"d": 0})
        save_snapshot(snap_dir, snap2)
        self.assertEqual(load_snapshot(snap_dir)["version"], 2)
        # Directory holds a single whole-replacement snapshot file.
        self.assertEqual(os.listdir(snap_dir), [SNAPSHOT_FILE])

    def test_corrupt_checksum_detected(self):
        snap_dir = os.path.join(self.tmp, "snap2")
        snap = build_snapshot(1, {"k": 1}, {}, {})
        save_snapshot(snap_dir, snap)
        path = os.path.join(snap_dir, SNAPSHOT_FILE)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["checksum"] = "0" * 64
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        with self.assertRaises(BusinessError) as ctx:
            load_snapshot(snap_dir)
        self.assertEqual(ctx.exception.code, ErrorCode.E_SNAPSHOT_CORRUPT)

    def test_downgrade_rejected(self):
        snap_dir = os.path.join(self.tmp, "snap3")
        save_snapshot(snap_dir, build_snapshot(3, {}, {}, {}))
        with self.assertRaises(BusinessError) as ctx:
            save_snapshot(snap_dir, build_snapshot(2, {}, {}, {}))
        self.assertEqual(ctx.exception.code, ErrorCode.E_SNAPSHOT_CORRUPT)

    def test_malformed_json_detected(self):
        snap_dir = os.path.join(self.tmp, "snap4")
        os.makedirs(snap_dir, exist_ok=True)
        with open(os.path.join(snap_dir, SNAPSHOT_FILE), "w") as fh:
            fh.write("{not-json")
        with self.assertRaises(BusinessError) as ctx:
            load_snapshot(snap_dir)
        self.assertEqual(ctx.exception.code, ErrorCode.E_SNAPSHOT_CORRUPT)


class ConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmp, "samples")
        self.manifest = generate_sample_bundle(
            self.data_dir, small_dialogs=1, large_dialogs=0
        )

    def test_report_passes_internal_self_checks(self):
        report = run_pipeline(self.manifest, ComponentConfig())
        dialog = report["dialogs"][0]
        # Segments are contiguous and labels cover every frame.
        self.assertEqual(len(dialog["diarization"]["frame_labels"]),
                         dialog["frame_count"])
        prev_end = 0
        for seg in dialog["segments"]:
            self.assertGreaterEqual(seg["start_frame"], prev_end)
            prev_end = seg["end_frame"]
        for seg in dialog["segments"]:
            self.assertLessEqual(seg["valence_ci"][0], seg["valence_ci"][1])
            self.assertLessEqual(seg["arousal_ci"][0], seg["arousal_ci"][1])


if __name__ == "__main__":
    unittest.main()
