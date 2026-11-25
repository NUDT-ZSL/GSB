from __future__ import annotations

import threading
import unittest

from llm_sampling_diag.errors import DiagnosticError, ErrorCode
from llm_sampling_diag.snapshots import SnapshotManager


class SnapshotTests(unittest.TestCase):
    def test_whole_snapshot_replacement(self) -> None:
        manager = SnapshotManager()
        first = manager.commit("task", 1, {"cases": ["a"], "config": {"dtype": "float64"}})
        second = manager.commit("task", 2, {"cases": ["a", "b"], "config": {"dtype": "float32"}})
        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)
        self.assertEqual(manager.get("task").payload, {"cases": ["a", "b"], "config": {"dtype": "float32"}})
        self.assertNotEqual(first.checksum, second.checksum)
        self.assertEqual([item["version"] for item in manager.history("task")], [1, 2])

    def test_returned_payload_is_a_copy(self) -> None:
        manager = SnapshotManager()
        payload = {"nested": {"value": 1}}
        snapshot = manager.commit("parameter", 1, payload)
        payload["nested"]["value"] = 99
        self.assertEqual(snapshot.payload["nested"]["value"], 1)
        retrieved = manager.get("parameter")
        retrieved.payload["nested"]["value"] = 7
        self.assertEqual(manager.get("parameter").payload["nested"]["value"], 1)

    def test_rollback_is_rejected(self) -> None:
        manager = SnapshotManager()
        manager.commit("result", 2, {"sequence": [1, 2]})
        with self.assertRaises(DiagnosticError) as context:
            manager.commit("result", 1, {"sequence": [1]})
        error = context.exception
        self.assertEqual(error.code, ErrorCode.E_SNAPSHOT_ROLLBACK)
        self.assertEqual(error.details["current_version"], 2)

    def test_same_version_different_checksum_conflicts(self) -> None:
        manager = SnapshotManager()
        manager.commit("task", 1, {"value": 1})
        with self.assertRaises(DiagnosticError) as context:
            manager.commit("task", 1, {"value": 2})
        self.assertEqual(context.exception.code, ErrorCode.E_SNAPSHOT_CONFLICT)

    def test_checksum_is_verified(self) -> None:
        manager = SnapshotManager()
        with self.assertRaises(DiagnosticError) as context:
            manager.commit("result", 1, {"value": 1}, checksum="0" * 64)
        self.assertEqual(context.exception.code, ErrorCode.E_SNAPSHOT_CHECKSUM_MISMATCH)

    def test_concurrent_same_snapshot_gets_stage_busy(self) -> None:
        entered = threading.Event()
        manager = SnapshotManager(hook=lambda kind: entered.set())
        errors: list[DiagnosticError] = []

        def first_writer() -> None:
            try:
                manager.commit("result", 1, {"value": [1, 2, 3]}, commit_delay_s=0.25)
            except DiagnosticError as error:
                errors.append(error)

        thread = threading.Thread(target=first_writer)
        thread.start()
        self.assertTrue(entered.wait(timeout=2.0))
        with self.assertRaises(DiagnosticError) as context:
            manager.commit("result", 1, {"value": [1, 2, 3]})
        self.assertEqual(context.exception.code, ErrorCode.E_STAGE_BUSY)
        thread.join(timeout=2.0)
        self.assertFalse(errors)
        self.assertEqual(manager.get("result").payload["value"], [1, 2, 3])

    def test_commit_bundle(self) -> None:
        manager = SnapshotManager()
        bundle = manager.commit_bundle(
            version=1,
            task_payload={"case_id": "case"},
            parameter_payload={"dtype": "float64"},
            result_payload={"sequence": [1]},
        )
        self.assertEqual(bundle["version"], 1)
        self.assertEqual(set(bundle["snapshots"]), {"task", "parameter", "result"})
        self.assertEqual(len(bundle["transaction_checksum"]), 64)


if __name__ == "__main__":
    unittest.main()

