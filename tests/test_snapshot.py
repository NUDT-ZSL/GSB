from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from edgecache.errors import ReplayError
from edgecache.node import NodeCache
from edgecache.snapshot import SnapshotService, snapshot_checksum
from edgecache.source import SourceRuntime

from tests.helpers import FakeClock, FakeSource, bootstrap_ledger, policy_doc


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.ledger = bootstrap_ledger(root / "ledger.sqlite3")
        self.clock = FakeClock()
        self.source = FakeSource(self.ledger)
        self.cache = NodeCache(self.ledger, "edge-a", self.source, now_ms=self.clock.now)
        self.snapshot_dir = root / "snapshots"
        self.service = SnapshotService(self.ledger, self.snapshot_dir)
        for path, tenant in (
            ("/assets/base.css", "public"),
            ("/assets/app.css", "public"),
            ("/assets/theme.css", "public"),
        ):
            self.cache.fill(
                path=path,
                key_values={"tenant": tenant, "encoding": "identity"},
                expected_version=1,
            )
        plan = SourceRuntime(self.ledger).evolve(
            path="/assets/base.css",
            etag='"base-v2"',
            body="base v2 for snapshot\n",
            body_encoding="utf-8",
            expected_version=1,
        )
        ordered = [item["path"] for item in plan["affected"]]
        result = self.cache.refresh_paths(
            trigger_path="/assets/base.css",
            paths=ordered,
            batch_id=plan["batch_id"],
            expected_version=plan["ledger_version"],
        )
        self.ledger.record_propagation_node_results(
            plan["batch_id"],
            "edge-a",
            result["results"],
            plan["ledger_version"],
        )
        self.snapshot = self.service.save("checkpoint.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_second_snapshot_keeps_read_only_backup(self) -> None:
        second = self.service.save("checkpoint.json")
        self.assertIsNotNone(second["backup_path"])
        backup = Path(second["backup_path"])
        self.assertTrue(backup.exists())
        if os.name != "nt":
            self.assertEqual(backup.stat().st_mode & 0o777, 0o444)

    def test_restore_replaces_entries_and_derivations_wholesale(self) -> None:
        original_entries = self.ledger.list_entries()
        original_signature = sorted(
            (item["node_id"], item["cache_key"], item["etag"], item["entry_version"])
            for item in original_entries
        )
        self.ledger.update_policy(policy_doc(policy_version="policy-v3-extra", node_capacity=5))
        extra_version = self.ledger.current_version()
        self.cache.fill(
            path="/index.html",
            key_values={"tenant": "public", "encoding": "identity"},
            expected_version=extra_version,
        )
        self.cache.reset_for_policy(extra_version)
        self.cache.fill(
            path="/index.html",
            key_values={"tenant": "public", "encoding": "identity"},
            expected_version=extra_version,
        )
        report = self.service.restore(self.snapshot["path"])
        self.assertTrue(report["version_differences"]["changed"])
        self.assertFalse(report["replacement"]["incremental_merge_used"])
        self.assertTrue(report["replacement"]["derivations_replaced"])
        self.assertGreaterEqual(report["replay"]["verified_traces"], 3)
        active = self.ledger.active_state()
        self.assertEqual(active.policy_version, "policy-v1")
        self.assertEqual(active.content.effective["/assets/base.css"].etag, '"base-v2"')
        restored_signature = sorted(
            (item["node_id"], item["cache_key"], item["etag"], item["entry_version"])
            for item in self.ledger.list_entries()
        )
        self.assertEqual(restored_signature, original_signature)
        self.assertGreater(report["restored_ledger_version"], extra_version)

    def test_replay_mismatch_returns_e_replay_with_basis(self) -> None:
        snapshot = copy.deepcopy(self.service.load(self.snapshot["path"]))
        traces = snapshot["propagation_traces"]
        self.assertGreaterEqual(len(traces), 3)
        traces[1]["affected_path"], traces[2]["affected_path"] = (
            traces[2]["affected_path"],
            traces[1]["affected_path"],
        )
        snapshot["checksum"] = snapshot_checksum(snapshot)
        tampered = self.snapshot_dir / "tampered.json"
        tampered.write_text(json.dumps(snapshot), encoding="utf-8")
        loaded = self.service.load(tampered)
        with self.assertRaises(ReplayError) as caught:
            self.service.verify_replay(loaded)
        self.assertEqual(caught.exception.code, "E_REPLAY")
        self.assertIn("expected", caught.exception.details.get("basis", {}))

    def test_payload_checksum_mismatch_returns_e_replay(self) -> None:
        snapshot = self.service.load(self.snapshot["path"])
        snapshot["content_set_hash"] = "tampered"
        tampered = self.snapshot_dir / "checksum-broken.json"
        tampered.write_text(json.dumps(snapshot), encoding="utf-8")
        with self.assertRaises(ReplayError) as caught:
            self.service.load(tampered)
        self.assertEqual(caught.exception.code, "E_REPLAY")
        self.assertIn("checksum", caught.exception.location or "")


if __name__ == "__main__":
    unittest.main()
