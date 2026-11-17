from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from alarm_domain.errors import DomainError
from alarm_domain.session import AlarmSession
from alarm_domain.snapshot import (
    SnapshotStore,
    replay_snapshot,
    restore_snapshot,
)
from alarm_domain.util import sha256_json

from .helpers import CHANGESET, EVENTS, RULES, TOPOLOGY


class SnapshotTests(unittest.TestCase):
    def test_restore_replaces_state_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.sqlite3"
            session = AlarmSession.from_files(RULES, TOPOLOGY, EVENTS, window_ms=500)
            cold_hash = session.analysis_hash
            cold_rule_version = session.rules_version
            store = SnapshotStore(path)
            try:
                cold = store.save(session.snapshot_payload(), label="cold")
                session.apply_changeset(CHANGESET)
                warm = store.save(session.snapshot_payload(), label="warm")
                self.assertNotEqual(cold.rules_version, warm.rules_version)
                before = store.list_snapshots()
                self.assertTrue(all(item.read_only for item in before))
                restored, report = restore_snapshot(
                    store, cold.snapshot_id, current_session=session
                )
                after = store.list_snapshots()
            finally:
                store.close()
        self.assertTrue(report["version_diff"]["changed_any"])
        self.assertTrue(report["version_diff"]["dimensions"]["rules"]["changed"])
        self.assertTrue(report["whole_state_replaced"])
        self.assertFalse(report["incremental_merge_used"])
        self.assertTrue(report["replay"]["match"])
        self.assertEqual(restored.analysis_hash, cold_hash)
        self.assertEqual(restored.rules_version, cold_rule_version)
        self.assertEqual([item.snapshot_id for item in after], [cold.snapshot_id, warm.snapshot_id])
        self.assertTrue(after[0].is_current)
        self.assertFalse(after[1].is_current)
        self.assertTrue(all(item.read_only for item in after))

    def test_missing_snapshot_versions_are_e_snap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "missing.sqlite3")
            try:
                session = AlarmSession.from_files(RULES, TOPOLOGY, EVENTS, window_ms=500)
                info = store.save(session.snapshot_payload(), omit_versions=True)
                with self.assertRaises(DomainError) as caught_restore:
                    restore_snapshot(store, info.snapshot_id, current_session=session)
                with self.assertRaises(DomainError) as caught_replay:
                    replay_snapshot(store, info.snapshot_id)
            finally:
                store.close()
        self.assertEqual(caught_restore.exception.code, "E_SNAP")
        self.assertEqual(
            caught_restore.exception.detail["missing_fields"],
            ["rules_version", "topology_version"],
        )
        self.assertEqual(caught_replay.exception.code, "E_REPLAY")

    def test_cursor_ahead_is_e_replay_with_basis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "ahead.sqlite3")
            try:
                session = AlarmSession.from_files(RULES, TOPOLOGY, EVENTS, window_ms=500)
                payload = session.snapshot_payload()
                payload["event_cursor"] = len(payload["events"]) + 1
                info = store.save(payload)
                with self.assertRaises(DomainError) as caught:
                    restore_snapshot(store, info.snapshot_id, current_session=session)
            finally:
                store.close()
        self.assertEqual(caught.exception.code, "E_REPLAY")
        self.assertIn("ahead", caught.exception.message)
        self.assertEqual(caught.exception.detail["event_cursor"], 11)
        self.assertEqual(caught.exception.detail["event_count"], 10)

    def test_tampered_cluster_conclusion_is_e_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tampered.sqlite3"
            store = SnapshotStore(db_path)
            try:
                session = AlarmSession.from_files(RULES, TOPOLOGY, EVENTS, window_ms=500)
                info = store.save(session.snapshot_payload())
            finally:
                store.close()
            payload = json.loads(
                sqlite3.connect(db_path)
                .execute("SELECT payload FROM snapshots WHERE snapshot_id = ?", (info.snapshot_id,))
                .fetchone()[0]
            )
            payload["clusters"][0]["root_cause"]["selected_service"] = "gateway"
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection = sqlite3.connect(db_path)
            connection.execute(
                "UPDATE snapshots SET payload = ?, payload_sha = ? WHERE snapshot_id = ?",
                (encoded, sha256_json(payload), info.snapshot_id),
            )
            connection.commit()
            connection.close()
            store = SnapshotStore(db_path)
            try:
                with self.assertRaises(DomainError) as caught:
                    replay_snapshot(store, info.snapshot_id)
            finally:
                store.close()
        self.assertEqual(caught.exception.code, "E_REPLAY")
        self.assertIn("first_difference_cluster_id", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
