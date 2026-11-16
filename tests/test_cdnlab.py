"""Local deterministic test suite for the cdnlab workbench (stdlib only)."""

import json
import os
import shutil
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdnlab.config import load_policy, parse_policy, SUPPORTED_KEY_FIELDS
from cdnlab.edge import EdgeNode, CacheStore, CacheEntry
from cdnlab.errors import E_PORT, E_CONFIG, E_STALE, E_UPSTREAM, E_EVICT, E_REPLAY, CdnError
from cdnlab.orchestrator import Orchestrator, find_free_ports
from cdnlab.origin import OriginState
from cdnlab.protocol import JsonLineServer
from cdnlab.replay import replay_snapshot

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO_ROOT, "samples", "cache_policy.json")
CONTENT = os.path.join(REPO_ROOT, "samples", "content")


def base_config():
    return {
        "node_capacity": 2,
        "eviction": "lru",
        "stale_on_upstream_error": True,
        "routes": [
            {"path": "/a.txt", "ttl": 3, "key_fields": ["path"]},
            {"path": "/b.txt", "ttl": 5, "key_fields": ["path", "lang"]},
        ],
    }


class ConfigValidationTest(unittest.TestCase):
    def test_valid_config_loads(self):
        policy = load_policy(CONFIG)
        self.assertEqual(policy.node_capacity, 3)
        self.assertEqual(policy.eviction, "lru")
        self.assertEqual(len(policy.routes), 3)
        self.assertEqual(len(policy.config_version), 16)

    def test_config_version_stable(self):
        self.assertEqual(load_policy(CONFIG).config_version,
                         load_policy(CONFIG).config_version)

    def _expect_e_config(self, mutate, location_part):
        data = base_config()
        mutate(data)
        with self.assertRaises(CdnError) as ctx:
            parse_policy(data)
        self.assertEqual(ctx.exception.code, E_CONFIG)
        self.assertIn(location_part, ctx.exception.location)

    def test_invalid_sample_file_rejected(self):
        with self.assertRaises(CdnError) as ctx:
            load_policy(os.path.join(REPO_ROOT, "samples", "cache_policy.invalid.json"))
        self.assertEqual(ctx.exception.code, E_CONFIG)
        self.assertEqual(ctx.exception.location, "node_capacity")

    def test_ttl_must_be_positive_int(self):
        self._expect_e_config(lambda d: d["routes"][0].update(ttl=0), "routes[0].ttl")
        self._expect_e_config(lambda d: d["routes"][0].update(ttl=-2), "routes[0].ttl")
        self._expect_e_config(lambda d: d["routes"][0].update(ttl=True), "routes[0].ttl")
        self._expect_e_config(lambda d: d["routes"][0].update(ttl="3"), "routes[0].ttl")

    def test_capacity_must_be_positive_int(self):
        self._expect_e_config(lambda d: d.update(node_capacity=0), "node_capacity")
        self._expect_e_config(lambda d: d.update(node_capacity=-1), "node_capacity")

    def test_key_fields_unique_and_supported(self):
        self._expect_e_config(
            lambda d: d["routes"][0].update(key_fields=["path", "path"]),
            "routes[0].key_fields")
        self._expect_e_config(
            lambda d: d["routes"][0].update(key_fields=["path", "region"]),
            "routes[0].key_fields")
        self._expect_e_config(
            lambda d: d["routes"][0].update(key_fields=[]),
            "routes[0].key_fields")

    def test_duplicate_path_rejected(self):
        def mutate(d):
            d["routes"].append({"path": "/a.txt", "ttl": 2, "key_fields": ["path"]})
        self._expect_e_config(mutate, "routes[2].path")

    def test_eviction_rule_restricted(self):
        self._expect_e_config(lambda d: d.update(eviction="random"), "eviction")


class CacheStoreTest(unittest.TestCase):
    def _entry(self, key, tick=0, ttl=10):
        return CacheEntry((("path", key),), key, {}, "body", '"e"', "LM", 1, tick, ttl)

    def test_lru_eviction(self):
        store = CacheStore(2, "lru")
        store.put("k1", self._entry("k1"), tick=1)
        store.put("k2", self._entry("k2"), tick=2)
        store.get("k1", tick=3)  # k1 most recently used
        evictions = store.put("k3", self._entry("k3"), tick=4)
        self.assertEqual([e["key"] for e in evictions], ["k2"])
        self.assertIn("k1", store.entries)

    def test_lfu_eviction(self):
        store = CacheStore(2, "lfu")
        store.put("k1", self._entry("k1"), tick=1)
        store.put("k2", self._entry("k2"), tick=2)
        store.get("k1", tick=3)
        store.get("k1", tick=4)
        store.get("k2", tick=5)
        evictions = store.put("k3", self._entry("k3"), tick=6)
        self.assertEqual([e["key"] for e in evictions], ["k2"])

    def test_fifo_eviction(self):
        store = CacheStore(2, "fifo")
        store.put("k1", self._entry("k1"), tick=1)
        store.put("k2", self._entry("k2"), tick=2)
        store.get("k1", tick=3)
        evictions = store.put("k3", self._entry("k3"), tick=4)
        self.assertEqual([e["key"] for e in evictions], ["k1"])

    def test_single_clock_ttl_decay(self):
        entry = self._entry("k1", tick=10, ttl=5)
        self.assertEqual(entry.remaining_ttl(10), 5)
        self.assertEqual(entry.remaining_ttl(13), 2)
        self.assertEqual(entry.remaining_ttl(15), 0)
        self.assertTrue(entry.is_fresh(14))
        self.assertFalse(entry.is_fresh(15))


class EdgeNodeTest(unittest.TestCase):
    """In-process edge against an in-process origin (no subprocesses)."""

    @classmethod
    def setUpClass(cls):
        cls.origin_state = OriginState(CONTENT)
        cls.origin_server = JsonLineServer(0, cls.origin_state.handle, name="test-origin")
        cls.origin_port = cls.origin_server.start()

    @classmethod
    def tearDownClass(cls):
        cls.origin_server.stop()

    def setUp(self):
        self.policy = parse_policy(base_config())
        self.node = EdgeNode("edge-0", self.policy, self.origin_port)
        # Map sample routes onto real sample files.
        self.origin_state.contents["/a.txt"] = "alpha-v1"
        self.origin_state.versions["/a.txt"] = 1
        self.origin_state.contents["/b.txt"] = "bravo-v1"
        self.origin_state.versions["/b.txt"] = 1

    def _req(self, path, fields=None, tick=0, force=False):
        return self.node.handle({
            "op": "request", "path": path, "fields": fields or {},
            "tick": tick, "force_validate": force,
        })

    def test_cold_miss_then_fresh_hit_same_entry(self):
        r1 = self._req("/a.txt", tick=0)
        r2 = self._req("/a.txt", tick=1)
        self.assertEqual(r1["outcome"], "MISS_COLD")
        self.assertEqual(r2["outcome"], "HIT_FRESH")
        self.assertEqual(r1["etag"], r2["etag"])
        self.assertEqual(r2["entry_version"], r1["entry_version"])
        self.assertEqual(r2["remaining_ttl"], 2)  # ttl=3, one tick elapsed

    def test_key_composition_separates_entries(self):
        zh = self._req("/b.txt", {"lang": "zh"}, tick=0)
        en = self._req("/b.txt", {"lang": "en"}, tick=1)
        self.assertEqual(zh["outcome"], "MISS_COLD")
        self.assertEqual(en["outcome"], "MISS_COLD")
        self.assertNotEqual(zh["key"], en["key"])
        again = self._req("/b.txt", {"lang": "zh"}, tick=2)
        self.assertEqual(again["outcome"], "HIT_FRESH")

    def test_key_collision_warning(self):
        self._req("/a.txt", {"device": "pc"}, tick=0)
        # /a.txt keys on path only; different unkeyed field -> collision warning.
        r = self._req("/a.txt", {"device": "mobile"}, tick=1)
        self.assertEqual(r["outcome"], "HIT_FRESH")
        self.assertIn("KEY_COLLISION", r["warnings"])

    def test_ttl_expiry_triggers_revalidation(self):
        self._req("/a.txt", tick=0)
        r = self._req("/a.txt", tick=3)  # ttl=3 expired
        self.assertEqual(r["outcome"], "REVALIDATED")
        self.assertEqual(r["revalidate_reason"], "expired")

    def test_origin_update_replaces_entry_and_bumps_version(self):
        r1 = self._req("/a.txt", tick=0)
        self.origin_state.handle({"op": "update", "path": "/a.txt", "content": "alpha-v2"})
        r2 = self._req("/a.txt", tick=1, force=True)
        self.assertEqual(r2["outcome"], "REVALIDATED")
        self.assertEqual(r2["entry_version"], r1["entry_version"] + 1)
        self.assertNotEqual(r1["etag"], r2["etag"])

    def test_unreachable_marks_stale_and_serves_marked(self):
        self._req("/a.txt", tick=0)
        self.origin_state.reachable = False
        try:
            r = self._req("/a.txt", tick=3, force=True)
            self.assertEqual(r["outcome"], "HIT_STALE")
            self.assertEqual(r["code"], E_UPSTREAM)
            self.assertTrue(r["stale"])
            self.assertEqual(r["consistency"], "STALE_SERVED")
            # Stale entry must never be served as a fresh hit.
            r2 = self._req("/a.txt", tick=4)
            self.assertNotEqual(r2["outcome"], "HIT_FRESH")
        finally:
            self.origin_state.reachable = True
        r3 = self._req("/a.txt", tick=5)
        self.assertEqual(r3["outcome"], "REVALIDATED")
        self.assertFalse(r3["stale"])

    def test_eviction_pressure_forces_miss_evicted(self):
        self._req("/b.txt", {"lang": "zh"}, tick=0)
        self._req("/b.txt", {"lang": "en"}, tick=1)  # capacity=2 reached
        self._req("/b.txt", {"lang": "ja"}, tick=2)  # evicts zh (lru)
        r = self._req("/b.txt", {"lang": "zh"}, tick=3)
        self.assertEqual(r["outcome"], "MISS_EVICTED")


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cdnlab-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _orch(self, ports=None):
        return Orchestrator(
            CONFIG, CONTENT, os.path.join(self.tmp, "s.sqlite"),
            ports=ports or find_free_ports(4),
        )

    def test_port_occupied_raises_e_port_and_cleans_up(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ports = find_free_ports(4)
        blocker.bind(("127.0.0.1", ports[1]))
        blocker.listen(1)
        try:
            orch = self._orch(ports=ports)
            with self.assertRaises(CdnError) as ctx:
                orch.start()
            self.assertEqual(ctx.exception.code, E_PORT)
            self.assertEqual(orch.processes, [])  # no orphans left behind
        finally:
            blocker.close()

    def test_injections_record_business_codes(self):
        with self._orch() as orch:
            orch.request(0, "/news.txt")
            orch.inject_origin_update("/news.txt", "news v2 injected\n")
            orch.inject_unreachable(True)
            r = orch.request(0, "/news.txt", force_validate=True)
            self.assertEqual(r["code"], E_UPSTREAM)
            self.assertEqual(r["outcome"], "HIT_STALE")
            orch.inject_unreachable(False)
            orch.inject_eviction_pressure(1, "/news.txt", 4)
            codes = [row["code"] for row in orch.store.load_trace() if row["kind"] == "injection"]
            self.assertEqual(codes, [E_STALE, E_UPSTREAM, E_UPSTREAM, E_EVICT])

    def test_snapshot_restore_and_replay_reproducible(self):
        traces = []
        for run in range(2):
            with self._orch() as orch:
                orch.request(0, "/news.txt")
                orch.request(0, "/news.txt")
                orch.inject_origin_update("/news.txt", "news v2 injected\n")
                orch.request(0, "/news.txt", force_validate=True)
                orch.request(1, "/article.txt", {"lang": "zh"})
                snapshot_id = orch.save_snapshot("s")
                verdict = replay_snapshot(orch, snapshot_id)
                self.assertTrue(verdict["ok"], verdict)
                self.assertEqual(verdict["compared"], 4)
                trace = [
                    {k: row[k] for k in ("tick", "kind", "node", "action", "payload", "result", "code")}
                    for row in orch.store.load_trace()
                ]
                traces.append(trace)
        # Identical input sequence -> identical hit attribution and conclusions.
        self.assertEqual(traces[0], traces[1])

    def test_replay_reports_first_diff_on_config_mismatch(self):
        with self._orch() as orch:
            orch.request(0, "/news.txt")
            snapshot_id = orch.save_snapshot("s")
            # Forge a divergent current config version.
            orch.policy.config_version = "deadbeefdeadbeef"
            verdict = replay_snapshot(orch, snapshot_id)
            self.assertFalse(verdict["ok"])
            self.assertEqual(verdict["code"], E_REPLAY)
            self.assertEqual(verdict["reason"], "config_version_mismatch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
