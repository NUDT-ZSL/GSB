from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sre_burnrate import (
    ReplayError,
    SnapshotError,
    evaluate_file,
    load_snapshot,
    replay_snapshot,
    restore_snapshot,
    save_snapshot,
)

from tests.helpers import assert_no_floats, make_config, simple_records, write_ndjson


class SnapshotReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_path = write_ndjson(
            self.root / "data.ndjson", simple_records(40, failure_burst=True)
        )
        self.config = make_config(long_minutes=8, short_minutes=3, step_minutes=2)
        self.session = evaluate_file(self.data_path, self.config, data_version="dataset-v1")
        self.snapshot_path = self.root / "session.snapshot.json"
        save_snapshot(self.session, self.snapshot_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_snapshot_round_trip_preserves_all_windows_and_alerts(self) -> None:
        snapshot = load_snapshot(self.snapshot_path)
        restored = snapshot.session
        self.assertEqual(restored.data_version, "dataset-v1")
        self.assertEqual(restored.data_sha256, self.session.data_sha256)
        self.assertEqual(len(restored.long_windows), len(self.session.long_windows))
        self.assertEqual(
            [window.to_dict() for window in restored.long_windows],
            [window.to_dict() for window in self.session.long_windows],
        )
        self.assertEqual(
            [step.to_dict() for step in restored.steps],
            [step.to_dict() for step in self.session.steps],
        )
        self.assertEqual(restored.final_alert_state, self.session.final_alert_state)
        payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        assert_no_floats(self, payload)

    def test_restore_reports_version_difference_and_never_merges(self) -> None:
        restored = restore_snapshot(
            self.snapshot_path,
            current_data_version="dataset-v2",
            current_data_sha256="0" * 64,
        )
        self.assertEqual(restored.report.action, "full_replace")
        self.assertFalse(restored.report.merged)
        self.assertEqual(
            [item["field"] for item in restored.report.version_differences],
            ["data_version", "data_sha256"],
        )
        self.assertEqual(restored.session.data_version, "dataset-v1")
        self.assertEqual(
            [window.to_dict() for window in restored.session.long_windows],
            [window.to_dict() for window in self.session.long_windows],
        )
        self.assertEqual(restored.session.final_alert_state, self.session.final_alert_state)

    def test_replay_success_is_bit_exact(self) -> None:
        restored = restore_snapshot(self.snapshot_path)
        report = replay_snapshot(
            self.snapshot_path,
            data_path=self.data_path,
            data_version=restored.session.data_version,
        )
        self.assertTrue(report.verified)
        self.assertEqual(report.steps_checked, len(self.session.steps))
        self.assertEqual(report.windows_checked, self.session.window_count)
        self.assertTrue(report.data_sha256_match)
        self.assertTrue(report.parameter_match)

    def test_replay_version_mismatch_uses_e_replay(self) -> None:
        with self.assertRaises(ReplayError) as context:
            replay_snapshot(
                self.snapshot_path,
                data_path=self.data_path,
                data_version="dataset-v2",
            )
        self.assertEqual(context.exception.code, "E_REPLAY")
        self.assertEqual(context.exception.first_mismatch_step, 0)
        self.assertEqual(context.exception.field, "data_version")
        self.assertEqual(context.exception.to_dict()["first_mismatch_step"], 0)
        self.assertEqual(context.exception.to_dict()["first_mismatch_field"], "data_version")

    def test_replay_window_parameter_mismatch_uses_e_replay(self) -> None:
        other_config = make_config(long_minutes=8, short_minutes=3, step_minutes=3)
        with self.assertRaises(ReplayError) as context:
            replay_snapshot(
                self.snapshot_path,
                data_path=self.data_path,
                expected_config=other_config,
            )
        self.assertEqual(context.exception.code, "E_REPLAY")
        self.assertEqual(context.exception.field, "config.windows.step_minutes")
        self.assertEqual(context.exception.first_mismatch_step, 0)

    def test_replay_detects_changed_data_content(self) -> None:
        changed_records = simple_records(40, failure_burst=True)
        changed_records[5]["requests"] += 1
        changed_path = write_ndjson(self.root / "changed.ndjson", changed_records)
        with self.assertRaises(ReplayError) as context:
            replay_snapshot(self.snapshot_path, data_path=changed_path)
        self.assertEqual(context.exception.code, "E_REPLAY")
        self.assertEqual(context.exception.field, "data_sha256")

    def test_replay_detects_tampered_snapshot_result(self) -> None:
        payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        tampered_window = payload["session"]["windows"]["long"][1]
        tampered_window["failed_requests"] += 1
        tampered_path = self.root / "tampered.snapshot.json"
        tampered_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ReplayError) as context:
            replay_snapshot(
                tampered_path,
                data_path=self.data_path,
                data_version=self.session.data_version,
            )
        self.assertEqual(context.exception.code, "E_REPLAY")
        self.assertEqual(context.exception.first_mismatch_step, 1)
        self.assertTrue(context.exception.field.startswith("windows.long[1]."))

    def test_malformed_snapshot_uses_e_snapshot(self) -> None:
        malformed = self.root / "malformed.snapshot.json"
        malformed.write_text('{"snapshot_format":"wrong"}', encoding="utf-8")
        with self.assertRaises(SnapshotError) as context:
            load_snapshot(malformed)
        self.assertEqual(context.exception.code, "E_SNAPSHOT")


if __name__ == "__main__":
    unittest.main()
