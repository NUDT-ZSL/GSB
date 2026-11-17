from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from edgecache.config import Policy
from edgecache.errors import VersionError
from edgecache.ledger import Ledger
from edgecache.node import NodeCache
from edgecache.source import SourceRuntime

from tests.helpers import FakeClock, FakeSource, bootstrap_ledger, policy_doc


class CacheBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "ledger.sqlite3")
        self.ledger = bootstrap_ledger(self.db_path)
        self.clock = FakeClock()
        self.source = FakeSource(self.ledger)
        self.cache = NodeCache(self.ledger, "edge-test", self.source, now_ms=self.clock.now)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fill(self, path: str, tenant: str = "public", version: int | None = None):
        return self.cache.fill(
            path=path,
            key_values={"tenant": tenant, "encoding": "identity"},
            expected_version=self.ledger.current_version() if version is None else version,
        )

    def test_hit_decrements_remaining_ttl_and_expiry_misses(self) -> None:
        result = self.fill("/index.html")
        self.assertEqual(result["entry_version"], 1)
        self.clock.advance(500)
        hit = self.cache.query(
            path="/index.html",
            key_values={"tenant": "public"},
            expected_version=1,
        )
        self.assertTrue(hit["hit"])
        self.assertEqual(hit["entry"]["remaining_ttl_ms"], 29_500)
        self.clock.advance(29_500)
        miss = self.cache.query(
            path="/index.html",
            key_values={"tenant": "public"},
            expected_version=1,
        )
        self.assertFalse(miss["hit"])
        self.assertEqual(miss["reason"], "expired")
        evictions = self.ledger.list_evictions("edge-test")
        self.assertTrue(any(item["reason"] == "ttl_expired" for item in evictions))

    def test_lru_capacity_eviction(self) -> None:
        doc = policy_doc(node_capacity=2, eviction_rule="lru")
        self.ledger.update_policy(doc)
        version = self.ledger.current_version()
        self.fill("/index.html", tenant="public", version=version)
        self.fill("/assets/theme.css", tenant="public", version=version)
        self.clock.advance(10)
        hit = self.cache.query(
            path="/index.html",
            key_values={"tenant": "public"},
            expected_version=version,
        )
        self.assertTrue(hit["hit"])
        self.clock.advance(10)
        self.fill("/assets/app.css", tenant="public", version=version)
        entries = self.ledger.list_entries("edge-test")
        paths = {item["path"] for item in entries}
        self.assertEqual(len(entries), 2)
        self.assertIn("/index.html", paths)
        self.assertIn("/assets/app.css", paths)
        evictions = self.ledger.list_evictions("edge-test")
        self.assertTrue(
            any(
                item["path"] == "/assets/theme.css"
                and item["reason"] == "capacity"
                and item["rule"] == "lru"
                for item in evictions
            )
        )

    def test_fifo_ignores_access_recency(self) -> None:
        doc = policy_doc(node_capacity=2, eviction_rule="fifo")
        self.ledger.update_policy(doc)
        version = self.ledger.current_version()
        self.fill("/index.html", version=version)
        self.clock.advance(10)
        self.fill("/assets/theme.css", version=version)
        self.clock.advance(50)
        self.cache.query(
            path="/index.html",
            key_values={"tenant": "public"},
            expected_version=version,
        )
        self.clock.advance(10)
        self.fill("/assets/app.css", version=version)
        paths = {item["path"] for item in self.ledger.list_entries("edge-test")}
        self.assertNotIn("/index.html", paths)
        self.assertIn("/assets/theme.css", paths)
        self.assertIn("/assets/app.css", paths)

    def test_content_evolution_refreshes_versions_and_descendants(self) -> None:
        self.fill("/assets/base.css")
        self.fill("/assets/app.css")
        self.fill("/assets/app.manifest.json")
        version = self.ledger.current_version()
        source_runtime = SourceRuntime(self.ledger)
        plan = source_runtime.evolve(
            path="/assets/base.css",
            etag='"base-v2"',
            body="changed base bytes\n",
            body_encoding="utf-8",
            expected_version=version,
        )
        ordered = [item["path"] for item in plan["affected"]]
        self.assertEqual(
            ordered,
            ["/assets/base.css", "/assets/app.css", "/assets/app.manifest.json"],
        )
        refreshed = self.cache.refresh_paths(
            trigger_path="/assets/base.css",
            paths=ordered,
            batch_id=plan["batch_id"],
            expected_version=plan["ledger_version"],
        )
        self.assertEqual(refreshed["ordered_paths"], ordered)
        entries = self.ledger.list_entries("edge-test")
        base = next(item for item in entries if item["path"] == "/assets/base.css")
        self.assertEqual(base["etag"], '"base-v2"')
        self.assertEqual(base["entry_version"], 2)
        app = next(item for item in entries if item["path"] == "/assets/app.css")
        self.assertEqual(app["entry_version"], 2)
        app_manifest = next(item for item in entries if item["path"] == "/assets/app.manifest.json")
        self.assertEqual(app_manifest["entry_version"], 2)
        self.assertEqual(plan["ledger_version"], 2)

    def test_stale_ledger_version_is_rejected(self) -> None:
        self.fill("/index.html")
        old_version = self.ledger.current_version()
        self.ledger.update_policy(policy_doc(policy_version="policy-v1-1"))
        with self.assertRaises(VersionError) as caught:
            self.cache.query(
                path="/index.html",
                key_values={"tenant": "public"},
                expected_version=old_version,
            )
        self.assertEqual(caught.exception.code, "E_VERSION")

    def test_policy_reset_removes_all_entries(self) -> None:
        self.fill("/index.html")
        self.fill("/assets/theme.css")
        self.ledger.update_policy(policy_doc(policy_version="policy-v2-test", node_capacity=2))
        result = self.cache.reset_for_policy(self.ledger.current_version())
        self.assertEqual(result["removed_entries"], 2)
        self.assertEqual(self.ledger.list_entries("edge-test"), [])
        self.assertTrue(
            any(item["reason"] == "policy_replace" for item in self.ledger.list_evictions("edge-test"))
        )

    def test_pending_intent_becomes_unknown_after_recovery(self) -> None:
        version = self.ledger.current_version()
        intent = self.cache.simulate_orphan_write(
            path="/index.html",
            key_values={"tenant": "public"},
            expected_version=version,
        )
        recovery = self.ledger.recover_node("edge-test")
        self.assertTrue(recovery["rerun_required"])
        self.assertEqual(recovery["unknown_intents"][0]["intent_id"], intent["intent_id"])
        self.assertEqual(recovery["unknown_intents"][0]["status"], "unknown")
        statuses = {
            item["intent_id"]: item["status"]
            for item in self.ledger.list_write_intents("edge-test")
        }
        self.assertEqual(statuses[intent["intent_id"]], "unknown")

    def test_existing_ledger_restart_ignores_bootstrap_documents(self) -> None:
        before = self.ledger.active_state()
        active, created = self.ledger.bootstrap({"not": "a policy"}, {"not": "content"})
        self.assertFalse(created)
        self.assertEqual(active.ledger_version, before.ledger_version)
        self.assertEqual(active.content_set_hash, before.content_set_hash)


if __name__ == "__main__":
    unittest.main()
