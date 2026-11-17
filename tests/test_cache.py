from __future__ import annotations

import unittest

from tests.helpers import SAMPLES

from shardrouter.cache import RouteCacheKey, RoutingCache
from shardrouter.config import load_config
from shardrouter.errors import E_CACHE
from shardrouter.predicates import normalize_predicate
from shardrouter.session import RouterSession


class CacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(SAMPLES / "config.json")

    def key(self, name: str, version: int = 1, ring: int = 0) -> RouteCacheKey:
        return RouteCacheKey(
            table="orders",
            normalized_predicate=name,
            rule_version=version,
            ring_version=ring,
        )

    def test_miss_then_hit(self) -> None:
        cache = RoutingCache(2)
        key = self.key("tenant_id = 7")
        self.assertEqual(cache.lookup(key, current_rule_version=1, current_ring_version=0).status, "miss")
        cache.put(key, {"plan": 1})
        self.assertEqual(cache.lookup(key, current_rule_version=1, current_ring_version=0).status, "hit")

    def test_version_mismatch_returns_e_cache_and_removes_entry(self) -> None:
        cache = RoutingCache(2)
        key = self.key("tenant_id = 7")
        cache.put(key, {"plan": 1})
        lookup = cache.lookup(key, current_rule_version=2, current_ring_version=0)
        self.assertEqual(lookup.status, "stale")
        self.assertEqual(lookup.code, E_CACHE)
        self.assertEqual(len(cache), 0)

    def test_eviction_is_deterministic_lru(self) -> None:
        first = RoutingCache(2)
        keys = [self.key("a"), self.key("b"), self.key("c")]
        for key in keys[:2]:
            first.put(key, {"plan": key.normalized_predicate})
        first.lookup(keys[0], current_rule_version=1, current_ring_version=0)
        first.put(keys[2], {"plan": "c"})
        self.assertEqual(
            [entry.key.normalized_predicate for entry in first.entries()],
            ["a", "c"],
        )

        second = RoutingCache(2)
        for key in keys[:2]:
            second.put(key, {"plan": key.normalized_predicate})
        second.lookup(keys[0], current_rule_version=1, current_ring_version=0)
        second.put(keys[2], {"plan": "c"})
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_route_cache_hit_uses_normalized_predicate(self) -> None:
        session = RouterSession(self.config)
        first = session.route_with_cache("orders", "tenant_id=7 AND region='east'")
        second = session.route_with_cache("orders", " TENANT_ID = 7 and REGION IN ('east') ")
        self.assertEqual(first["cache"]["status"], "miss")
        self.assertEqual(second["cache"]["status"], "hit")
        self.assertEqual(first["plan"], second["plan"])

    def test_session_recomputes_on_stale_recorded_version(self) -> None:
        session = RouterSession(self.config)
        table = session.table("orders")
        info = normalize_predicate(table, "tenant_id=7 AND region='east'")
        key = session._cache_key(table, info)
        plan = session.route("orders", "tenant_id=7 AND region='east'")
        session.cache.put(key, plan, recorded_rule_version=0, recorded_ring_version=0)
        outcome = session.route_with_cache("orders", "tenant_id=7 AND region='east'")
        self.assertEqual(outcome["cache"]["status"], "recomputed")
        self.assertEqual(outcome["cache"]["code"], E_CACHE)


if __name__ == "__main__":
    unittest.main()
