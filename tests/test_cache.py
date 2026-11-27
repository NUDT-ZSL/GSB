from __future__ import annotations

import unittest

from flight_compile_cache.cache import CompileCacheEngine
from flight_compile_cache.generator import generate_project
from flight_compile_cache.graph import version_changed_units


def request_by(project, *, model_id, version, segment=None, time_gt=None, time_lt=None):
    for request in project["requests"]:
        if request.get("model_id") != model_id:
            continue
        if request.get("model_version") != version:
            continue
        if time_gt is not None and request["time"] <= time_gt:
            continue
        if time_lt is not None and request["time"] >= time_lt:
            continue
        if segment is not None:
            shape = request["input_shapes"]
            if model_id == "delay_predict":
                actual = shape["flight_features"][0]
            else:
                actual = f"{shape['flight_slots'][0]},{shape['ground_tasks'][0]}"
            if str(actual) != str(segment):
                continue
        return request
    raise AssertionError("request not found")


class CacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = generate_project()

    def test_hit_and_miss_with_stable_artifacts(self) -> None:
        engine = CompileCacheEngine(self.project)
        first = request_by(
            self.project, model_id="delay_predict", version="v1", time_lt=20
        )
        record1 = engine.process_request(first)
        self.assertEqual(record1["cache_lookup"], "miss")
        record2 = engine.process_request(first)
        self.assertEqual(record2["cache_lookup"], "hit")
        self.assertEqual(record1["artifact_ids"], record2["artifact_ids"])
        self.assertEqual(record2["compile"]["compile_work"], 0)
        self.assertEqual(record1["chain"]["first_registration_source"], record2["chain"]["first_registration_source"])

    def test_lru_eviction_and_recompile_keeps_artifact_ids(self) -> None:
        engine = CompileCacheEngine(self.project, cache_config_override={"capacity": 1})
        req_a = request_by(self.project, model_id="delay_predict", version="v1", segment="3", time_lt=20)
        req_b = request_by(self.project, model_id="delay_predict", version="v1", segment="4", time_lt=40)
        first = engine.process_request(req_a)
        second = engine.process_request(req_b)
        self.assertEqual(len(second["evictions"]), 1)
        self.assertEqual(second["evictions"][0]["compile_key"], first["compile_key"])
        self.assertTrue(second["evictions"][0]["released_unit_ids"])
        third = engine.process_request(req_a)
        self.assertEqual(third["cache_lookup"], "miss")
        self.assertEqual(third["artifact_ids"], first["artifact_ids"])
        meta = engine.key_registry[first["compile_key"]]
        self.assertTrue(meta["artifact_ids_repeatable"])
        self.assertEqual(meta["times_compiled"], 2)
        self.assertGreaterEqual(meta["eviction_count"], 1)

    def test_lru_uses_recent_access_order(self) -> None:
        engine = CompileCacheEngine(self.project, cache_config_override={"capacity": 2})
        req3 = request_by(self.project, model_id="delay_predict", version="v1", segment="3", time_lt=20)
        req4 = request_by(self.project, model_id="delay_predict", version="v1", segment="4", time_lt=40)
        req5 = request_by(self.project, model_id="delay_predict", version="v1", segment="5", time_lt=80)
        engine.process_request(req3)
        key4 = engine.process_request(req4)["compile_key"]
        engine.process_request(req3)  # req3 becomes most recently used
        evicted = engine.process_request(req5)["evictions"]
        self.assertEqual([item["compile_key"] for item in evicted], [key4])

    def test_model_update_scope_and_unaffected_artifact_stability(self) -> None:
        engine = CompileCacheEngine(self.project, cache_config_override={"capacity": 8})
        delay_v1 = request_by(self.project, model_id="delay_predict", version="v1", time_lt=120)
        resource_v1 = request_by(
            self.project, model_id="resource_alloc", version="v1", time_lt=120
        )
        engine.process_request(delay_v1)
        resource_record = engine.process_request(resource_v1)
        old_delay_key = next(
            entry.compile_key
            for entry in engine.entries.values()
            if entry.model_id == "delay_predict"
        )
        old_artifacts = dict(engine.entries[old_delay_key].artifacts)
        event = next(item for item in self.project["events"] if item["id"] == "evt-001")
        report = engine.apply_event(event)
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["incremental_recompile_scope"], ["delay_mean"])
        self.assertTrue(report["unaffected_artifact_ids_stable"])
        self.assertTrue(report["other_model_keys_preserved"])
        self.assertNotIn(old_delay_key, engine.entries)
        new_entry = next(
            entry
            for entry in engine.entries.values()
            if entry.model_id == "delay_predict" and entry.model_version == "v2"
        )
        diff = version_changed_units(self.project, "delay_predict", "v1", "v2")
        for unit_id in diff["unaffected_units"]:
            self.assertEqual(
                old_artifacts[unit_id]["artifact_id"],
                new_entry.artifacts[unit_id]["artifact_id"],
                unit_id,
            )
        self.assertNotEqual(
            old_artifacts["delay_mean"]["artifact_id"],
            new_entry.artifacts["delay_mean"]["artifact_id"],
        )
        self.assertIn(resource_record["compile_key"], engine.entries)

    def test_numeric_baseline_comparison(self) -> None:
        engine = CompileCacheEngine(self.project)
        for request in self.project["requests"]:
            record = engine.process_request(request)
            if record.get("status") == "accepted":
                self.assertTrue(record["numeric_consistent"], record["request_id"])
                self.assertLessEqual(record["max_abs_error"], self.project["cache_config"]["numeric_tolerance"])
        summary = engine.summary()
        self.assertGreater(summary["baseline_compile_work"], summary["total_compile_work"])
        self.assertEqual(summary["inconsistent_requests"], 0)
        self.assertGreater(summary["shape_specialized_kernel_compiles"], 0)


if __name__ == "__main__":
    unittest.main()
