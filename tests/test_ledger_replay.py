from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from model_service_workbench.declaration import load_declaration, load_declaration_text
from model_service_workbench.errors import ReplayError, StateError
from model_service_workbench.workbench import Workbench


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "cluster.json"
MODIFIED = ROOT / "samples" / "cluster_modified.json"


class LedgerReplayTests(unittest.TestCase):
    def test_stale_version_snapshot_cannot_become_current(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workbench = Workbench(SAMPLE, root / "ledger.sqlite", root / "snapshots")
            try:
                workbench.load_declaration()
                first_run = workbench.run_all()["run_id"]
                snapshot = workbench.save_snapshot(first_run)
                workbench.load_declaration(MODIFIED)
                with self.assertRaises(StateError) as context:
                    workbench.restore_snapshot(snapshot["snapshot_id"])
                self.assertEqual(context.exception.code, "E_STATE")
                self.assertEqual(
                    context.exception.details["snapshot_version"],
                    1,
                )
                self.assertEqual(
                    context.exception.details["latest_declaration_version"],
                    2,
                )
            finally:
                workbench.close()

    def test_run_replay_snapshot_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workbench = Workbench(SAMPLE, root / "ledger.sqlite", root / "snapshots")
            try:
                workbench.load_declaration()
                run_id = workbench.run_all()["run_id"]
                replay = workbench.replay(run_id)
                self.assertTrue(replay["ok"])
                self.assertEqual(replay["minutes_replayed"], 8)
                snapshot = workbench.save_snapshot(run_id)
                original = workbench.ledger.get_decisions(run_id)

                restore = workbench.restore_snapshot(snapshot["snapshot_id"])
                restored_run = restore["restored_run_id"]
                restored = workbench.ledger.get_decisions(restored_run)
                self.assertTrue(restore["replay"]["ok"])
                self.assertEqual(restore["restored_minutes"], 8)
                self.assertEqual(
                    self._stable(original),
                    self._stable(restored),
                )
                replay_restored = workbench.replay(restored_run)
                self.assertTrue(replay_restored["ok"])
            finally:
                workbench.close()

    @staticmethod
    def _stable(minutes):
        return [
            {
                key: value
                for key, value in minute.items()
                if key not in ("committed_at", "is_stale")
            }
            for minute in minutes
        ]

    def test_forced_interruption_recovery_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger_path = root / "ledger.sqlite"
            snapshot_dir = root / "snapshots"
            first = Workbench(SAMPLE, ledger_path, snapshot_dir)
            try:
                first.load_declaration()
                run_id = first.run_all(limit_minutes=3)["run_id"]
                self.assertEqual(first.get_run(run_id)["completed_minutes"], 3)
                self.assertEqual(first.get_run(run_id)["status"], "running")
            finally:
                first.close()

            second = Workbench(SAMPLE, ledger_path, snapshot_dir)
            try:
                registration = second.load_declaration()
                self.assertFalse(registration.version_changed)
                report = second.recover(run_id)
                self.assertEqual(report["report_kind"], "recovery")
                self.assertEqual(report["recovered_minutes"], 3)
                self.assertEqual(report["pending_minutes"], [3, 4, 5, 6, 7])
                self.assertEqual(report["unknown_minutes"], [3, 4, 5, 6, 7])
                self.assertEqual(report["first_unknown_minute"], 3)
                self.assertTrue(report["requires_rerun"])
                self.assertFalse(report["exit_report_available"])

                resumed = second.resume(run_id)
                self.assertTrue(resumed["replay_before_rerun"]["ok"])
                self.assertTrue(resumed["replay_after_rerun"]["ok"])
                self.assertEqual(resumed["recovery"]["recovered_minutes"], 3)
                run = second.get_run(run_id)
                self.assertEqual(run["completed_minutes"], 8)
                self.assertEqual(run["status"], "completed")
            finally:
                second.close()

    def test_tampered_final_replicas_is_first_replay_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workbench = Workbench(SAMPLE, root / "ledger.sqlite", root / "snapshots")
            try:
                workbench.load_declaration()
                run_id = workbench.run_all()["run_id"]
                with workbench.ledger.connection:
                    workbench.ledger.connection.execute(
                        """
                        UPDATE service_decisions
                        SET final_replicas = final_replicas + 1
                        WHERE run_id = ? AND minute = 2 AND service = 'beta'
                        """,
                        (run_id,),
                    )
                with self.assertRaises(ReplayError) as context:
                    workbench.replay(run_id)
                error = context.exception
                self.assertEqual(error.code, "E_REPLAY")
                self.assertEqual(error.details["minute"], 2)
                self.assertEqual(error.details["service"], "beta")
                self.assertEqual(error.details["field"], "final_replicas")
                self.assertEqual(error.path, "$.runs.minutes[2].services[beta].final_replicas")
            finally:
                workbench.close()

    def test_declaration_version_mismatch_is_replay_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workbench = Workbench(SAMPLE, root / "ledger.sqlite", root / "snapshots")
            try:
                workbench.load_declaration()
                run_id = workbench.run_all()["run_id"]
                other = load_declaration(MODIFIED).with_version(2)
                with self.assertRaises(ReplayError) as context:
                    workbench.replay(run_id, supplied_declaration=other)
                self.assertEqual(context.exception.details["field"], "declaration_version")
                self.assertEqual(
                    context.exception.details["reason"],
                    "declaration_version_mismatch",
                )
            finally:
                workbench.close()

    def test_load_sequence_mismatch_is_replay_error_with_basis(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workbench = Workbench(SAMPLE, root / "ledger.sqlite", root / "snapshots")
            try:
                workbench.load_declaration()
                run_id = workbench.run_all()["run_id"]
                document = json.loads(SAMPLE.read_text(encoding="utf-8"))
                changed = copy.deepcopy(document)
                changed["services"][1]["requests"][3] = 44
                changed_declaration = load_declaration_text(
                    json.dumps(changed),
                    "changed.json",
                ).with_version(1)
                with self.assertRaises(ReplayError) as context:
                    workbench.replay(
                        run_id,
                        supplied_declaration=changed_declaration,
                    )
                error = context.exception
                self.assertEqual(error.code, "E_REPLAY")
                self.assertEqual(error.details["reason"], "load_sequence_mismatch")
                self.assertEqual(error.details["service"], "beta")
                self.assertEqual(error.details["minute"], 3)
                self.assertEqual(error.details["recorded"], 45)
                self.assertEqual(error.details["supplied"], 44)
            finally:
                workbench.close()


if __name__ == "__main__":
    unittest.main()
