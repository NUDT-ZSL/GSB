from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mm_router.cache import ContentAddressedCache, plan_invalidation
from mm_router.config_io import read_config
from mm_router.errors import E_FORMAT_CORRUPT, WorkbenchError
from mm_router.generate import generate_sample, read_requests
from mm_router.routing import RoutingPipeline


class RoutingCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sample = generate_sample(self.root / "sample", request_count=8)
        self.requests = read_requests(self.sample["requests"])
        self.config = read_config(self.sample["config_v1"]).config

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_route_is_byte_identical_across_cache_state(self) -> None:
        cache = ContentAddressedCache(self.root / "cache")
        pipeline = RoutingPipeline(cache)
        first = pipeline.execute(self.requests[0], self.config)
        second = pipeline.execute(self.requests[0], self.config)
        self.assertTrue(all(value == "recompute" for value in first.statuses().values()))
        self.assertTrue(all(value == "reuse" for value in second.statuses().values()))
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertGreater(first.total_operations, 0)
        for execution in first.node_executions:
            self.assertTrue(execution.artifact["operations"])
            self.assertTrue(execution.artifact["rounding_modes"])

    def test_cached_and_uncached_decisions_match(self) -> None:
        cached = RoutingPipeline(
            ContentAddressedCache(self.root / "cached")
        ).execute(self.requests[0], self.config)
        direct = RoutingPipeline(None).execute(self.requests[0], self.config)
        self.assertEqual(cached.canonical_bytes(), direct.canonical_bytes())

    def test_cache_keys_include_required_components(self) -> None:
        cache = ContentAddressedCache(self.root / "cache")
        decision = RoutingPipeline(cache).execute(self.requests[0], self.config)
        expected_digest = self.requests[0].input_digest()
        for execution in decision.node_executions:
            components = execution.key_components
            self.assertEqual(components["input_digest"], expected_digest)
            self.assertIn("upstream_artifact_hash", components)
            self.assertIn("threshold_config_version", components)
            self.assertEqual(components["route_budget"], self.config.route_budget)
            self.assertEqual(components["precision"], self.config.precision)
        self.assertEqual(
            decision.node_executions[0].key_components["threshold_config_version"],
            "0.0",
        )
        self.assertEqual(
            decision.node_executions[2].key_components["threshold_config_version"],
            self.config.version_token,
        )

    def test_threshold_generation_reuses_prefix_and_recomputes_suffix(self) -> None:
        evolved_requests = read_requests(self.sample["evolved_requests"])
        next_config = read_config(
            self.sample["config_v2"],
            previous_updated_at_ms=self.config.updated_at_ms,
        ).config
        cache = ContentAddressedCache(self.root / "cache")
        pipeline = RoutingPipeline(cache)
        for request in self.requests:
            pipeline.execute(request, self.config)
        plan = plan_invalidation(
            self.requests,
            evolved_requests,
            self.config,
            next_config,
        )
        decisions = [pipeline.execute(request, next_config) for request in evolved_requests]
        statuses = {item.request_id: item.statuses() for item in decisions}
        for node_id in plan.reused_nodes:
            request_id, node = node_id.split(":", 1)
            self.assertEqual(statuses[request_id][node], "reuse")
        for node_id in plan.recomputed_nodes:
            request_id, node = node_id.split(":", 1)
            self.assertEqual(statuses[request_id][node], "recompute")
        self.assertTrue(
            set(plan.reused_nodes).isdisjoint(set(plan.recomputed_nodes))
        )
        self.assertEqual(
            set(plan.reused_nodes).union(plan.recomputed_nodes),
            set(plan.covered_nodes),
        )

    def test_revision_change_starts_at_n1(self) -> None:
        changed = list(self.requests)
        changed[0] = changed[0].__class__(
            request_id=changed[0].request_id,
            prompt_text=changed[0].prompt_text,
            image_seed=changed[0].image_seed,
            text_revision=changed[0].text_revision + 1,
            image_revision=changed[0].image_revision,
        )
        plan = plan_invalidation(
            self.requests,
            changed,
            self.config,
            self.config,
        )
        self.assertEqual(plan.earliest_node_by_request[changed[0].request_id], "N1_FEATURE")
        self.assertEqual(len(plan.affected_request_ids), 1)
        self.assertEqual(len(plan.recomputed_nodes), 4)
        self.assertEqual(len(plan.reused_nodes), 0)

    def test_precision_and_budget_change_start_at_n1(self) -> None:
        for replacement in (
            self.config.replace(precision="fp64"),
            self.config.replace(route_budget=self.config.route_budget + 1),
        ):
            plan = plan_invalidation(
                self.requests[:1],
                self.requests[:1],
                self.config,
                replacement,
            )
            self.assertEqual(len(plan.recomputed_nodes), 4)
            self.assertEqual(len(plan.reused_nodes), 0)

    def test_cache_artifact_tamper_is_detected(self) -> None:
        cache = ContentAddressedCache(self.root / "cache")
        decision = RoutingPipeline(cache).execute(self.requests[0], self.config)
        execution = decision.node_executions[0]
        path = cache._path(execution.cache_key)
        envelope = __import__("json").loads(path.read_text(encoding="utf-8"))
        envelope["artifact"]["operations"]["quantize"] += 1
        path.write_text(
            __import__("json").dumps(envelope, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaises(WorkbenchError) as caught:
            cache.get(
                execution.cache_key,
                key_components=execution.key_components,
            )
        self.assertEqual(caught.exception.code, E_FORMAT_CORRUPT)


if __name__ == "__main__":
    unittest.main()

