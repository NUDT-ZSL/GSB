from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import SAMPLE, run_cli_json, setup_full_state


class WorkflowTests(unittest.TestCase):
    def test_snapshot_rollback_fork_gc_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            outputs = setup_full_state(state, gc=True)
            by_command = {item["command"]: item for item in outputs}
            rb1 = next(
                item for item in outputs if item.get("request_id") == "rb-001"
            )
            rb2 = next(
                item for item in outputs if item.get("request_id") == "rb-002"
            )
            rb3 = next(
                item for item in outputs if item.get("request_id") == "rb-003"
            )
            self.assertEqual(
                rb1["preserved_snapshot_ids"], ["snap-002", "snap-003"]
            )
            self.assertEqual(rb2["orphaned_snapshot_ids"], ["snap-003", "snap-004"])
            self.assertEqual(rb3["orphaned_snapshot_ids"], ["snap-002", "snap-005"])
            self.assertEqual(by_command["gc"]["reclaimed_blocks"], 8)

            status_result, status = run_cli_json(state, "status")
            self.assertEqual(status_result.returncode, 0)
            self.assertIsNotNone(status)
            statuses = {
                item["snapshot_id"]: item["status"]
                for item in status["snapshots"]
            }
            self.assertEqual(statuses["snap-base"], "alive")
            self.assertEqual(statuses["snap-001"], "alive")
            self.assertEqual(statuses["snap-006"], "alive")
            for orphan_id in ("snap-002", "snap-003", "snap-004", "snap-005"):
                self.assertEqual(statuses[orphan_id], "orphan")
            self.assertEqual(status["current_snapshot_id"], "snap-006")
            self.assertEqual(status["block_objects"]["unreferenced"], 0)
            orphan_ids = {
                item["snapshot_id"]
                for item in status["snapshots"]
                if item["status"] == "orphan"
            }
            for reference in status["reference_counts"]:
                for source in reference["sources"]:
                    if source["kind"] == "snapshot":
                        self.assertNotIn(source["id"], orphan_ids)

            verify_result, verify = run_cli_json(state, "verify")
            self.assertEqual(verify_result.returncode, 0, verify_result.stdout)
            self.assertIsNotNone(verify)
            self.assertTrue(verify["ok"])
            self.assertEqual(verify["errors"], [])

    def test_snapshot_and_rollback_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            result, _ = run_cli_json(state, "load")
            self.assertEqual(result.returncode, 0)
            first_result, first = run_cli_json(state, "snapshot", "--sequence", "1")
            second_result, second = run_cli_json(state, "snapshot", "--sequence", "1")
            self.assertEqual(first_result.returncode, 0)
            self.assertEqual(second_result.returncode, 0)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(second["status"], "already_committed")
            self.assertEqual(first["chain_version"], second["chain_version"])

            rollback_result, rollback = run_cli_json(
                state, "rollback", "--request-id", "rb-001"
            )
            repeated_result, repeated = run_cli_json(
                state, "rollback", "--request-id", "rb-001"
            )
            self.assertEqual(rollback_result.returncode, 0)
            self.assertEqual(repeated_result.returncode, 0)
            self.assertEqual(rollback, repeated)

            status_result, status = run_cli_json(state, "status")
            self.assertEqual(status_result.returncode, 0)
            self.assertIsNotNone(status)
            self.assertEqual(status["chain_version"], rollback["chain_version"])

    def test_block_digest_is_reused(self) -> None:
        from blockstore import BlockSnapshotStore
        from blockstore.model import load_sample

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            store = BlockSnapshotStore(state)
            sample = load_sample(Path("samples/fixed_sample.json"))
            store.load(sample)
            block_data = sample.volume.blocks[0].data
            before = len(store.storage.list_digests())
            digest = store.storage.put(block_data)
            after = len(store.storage.list_digests())
            self.assertEqual(digest, sample.volume.blocks[0].digest)
            self.assertEqual(before, after)
            self.assertTrue(store.storage.verify_file(digest, expected_size=16))

    def test_rollback_to_orphan_is_rejected_without_state_change(self) -> None:
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["rollbacks"].append(
            {
                "request_id": "rb-target-orphan",
                "target_snapshot_id": "snap-004",
                "clock_ms": 6500,
                "retain_after": False,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample_path = root / "sample.json"
            sample_path.write_text(json.dumps(raw), encoding="utf-8")
            state = root / "state"
            result, _ = run_cli_json(
                state, "load", "--sample", str(sample_path)
            )
            self.assertEqual(result.returncode, 0)
            for sequence in (1, 2, 3):
                result, _ = run_cli_json(
                    state, "snapshot", "--sequence", str(sequence)
                )
                self.assertEqual(result.returncode, 0)
            result, _ = run_cli_json(state, "rollback", "--request-id", "rb-001")
            self.assertEqual(result.returncode, 0)
            result, _ = run_cli_json(state, "snapshot", "--sequence", "4")
            self.assertEqual(result.returncode, 0)
            result, _ = run_cli_json(state, "rollback", "--request-id", "rb-002")
            self.assertEqual(result.returncode, 0)
            before = run_cli_json(state, "status")[1]
            self.assertIsNotNone(before)
            rejected_result, rejected = run_cli_json(
                state, "rollback", "--request-id", "rb-target-orphan"
            )
            self.assertNotEqual(rejected_result.returncode, 0)
            self.assertIsNotNone(rejected)
            self.assertEqual(rejected["error"]["code"], "SNAPSHOT_NOT_ALIVE")
            after = run_cli_json(state, "status")[1]
            self.assertIsNotNone(after)
            self.assertEqual(after["chain_version"], before["chain_version"])
            self.assertEqual(after["current_snapshot_id"], before["current_snapshot_id"])

    def test_verify_detects_missing_referenced_block_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            setup_full_state(state, gc=True)
            status = run_cli_json(state, "status")[1]
            self.assertIsNotNone(status)
            digest = status["reference_counts"][0]["digest"]
            (state / "blocks" / digest).unlink()
            verify_result, verify = run_cli_json(state, "verify")
            self.assertEqual(verify_result.returncode, 2)
            self.assertIsNotNone(verify)
            self.assertFalse(verify["ok"])
            self.assertIn(
                "MISSING_BLOCK_FILE",
                {error["code"] for error in verify["errors"]},
            )
