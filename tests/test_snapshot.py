from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import SAMPLES

from shardrouter.config import load_config
from shardrouter.errors import E_VERSION
from shardrouter.session import RouterSession
from shardrouter.util import canonical_json
from shardrouter.verification import canonical_text


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config_v1 = load_config(SAMPLES / "config.json")
        cls.config_v2 = load_config(SAMPLES / "config_v2.json")

    def test_snapshot_mismatch_whole_replaces_and_replays_byte_identically(self) -> None:
        session = RouterSession(self.config_v1)
        original = session.route("orders", "tenant_id=7 AND region='east'")
        session.route_unit("orders_lookup")
        session.route_unit("orders_rollup")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            session.save_snapshot(path)
            snapshot = RouterSession.load_snapshot(path)
        snapshot_cache = canonical_json(snapshot["cache"])
        session.apply_config(self.config_v2)
        session.route("orders", "tenant_id IN (7,8) AND region='east'")
        self.assertIn("orders_audit", session.config.unit_map())
        report = session.restore_snapshot(snapshot)
        self.assertEqual(report["mode"], "whole_replace")
        self.assertEqual(session.config.config_version, 1)
        self.assertNotIn("orders_audit", session.config.unit_map())
        self.assertEqual(canonical_json(session.cache.to_dict()), snapshot_cache)
        self.assertEqual(
            [difference["field"] for difference in report["differences"]],
            sorted(difference["field"] for difference in report["differences"]),
        )
        self.assertIn("config_version", {item["field"] for item in report["differences"]})
        self.assertIn("dependency_graph_version", {item["field"] for item in report["differences"]})
        replay = session.route("orders", "tenant_id=7 AND region='east'")
        self.assertEqual(canonical_text(original), canonical_text(replay))

    def test_metadata_version_mismatch_is_e_version(self) -> None:
        session = RouterSession(self.config_v1)
        snapshot = session.snapshot()
        with self.assertRaises(Exception) as raised:
            session.restore_snapshot(snapshot, expected_metadata_version=999)
        self.assertEqual(getattr(raised.exception, "code", None), E_VERSION)

    def test_snapshot_internal_metadata_mismatch_is_e_version(self) -> None:
        session = RouterSession(self.config_v1)
        snapshot = session.snapshot()
        snapshot["versions"]["metadata_version"] = 2
        with self.assertRaises(Exception) as raised:
            session.restore_snapshot(snapshot)
        self.assertEqual(getattr(raised.exception, "code", None), E_VERSION)

    def test_snapshot_history_is_replaced_not_merged(self) -> None:
        session = RouterSession(self.config_v1)
        session.route("ledger", "amount=100")
        snapshot = session.snapshot()
        history_length = len(snapshot["route_history"])
        session.apply_config(self.config_v2)
        session.route("orders", "tenant_id=7 AND region='east'")
        report = session.restore_snapshot(snapshot)
        self.assertEqual(report["route_history_entries"], history_length)
        self.assertEqual(session.history[0]["action"], "route")


if __name__ == "__main__":
    unittest.main()
