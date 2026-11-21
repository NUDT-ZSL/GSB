import os
import shutil
import tempfile
import unittest
from dataclasses import replace

from drift_monitor.cache import ContentStore
from drift_monitor.config_migration import upgrade_to_v3
from drift_monitor.config_reader import read_config
from drift_monitor.data import DriftInjection, generate_batch, generate_stream
from drift_monitor.engine import MonitoringEngine
from drift_monitor.errors import DriftError
from drift_monitor.hashing import canonicalize
from drift_monitor.resources import ResourceGuard, ResourceLimits
from drift_monitor.snapshot import AlertStore, SnapshotStore


CONFIG = """# DRIFT_MONITOR_CONFIG v1
income_score|numeric|1.2|4|true
session_length|numeric|1.1|4|true
device_class|categorical|0.18|4|true
region|categorical|0.2|4|true
"""


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = upgrade_to_v3(read_config(CONFIG))
        self.guard = ResourceGuard(ResourceLimits())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def engine(self, limits=None):
        limits = limits or ResourceLimits()
        return MonitoringEngine(
            config=self.config,
            cache=ContentStore(os.path.join(self.tmp, "cache")),
            snapshots=SnapshotStore(os.path.join(self.tmp, "snapshots")),
            alerts=AlertStore(os.path.join(self.tmp, "alerts")),
            guard=ResourceGuard(limits),
        )


class CacheAndDriftTests(EngineTestBase):
    def test_cache_reuse_and_replay_identical(self):
        engine = self.engine()
        stream = generate_stream(batch_count=6, sample_count=120)
        first_reports = [engine.evaluate_batch(batch).to_dict() for batch in stream]
        self.assertEqual(engine.cache.hits, 0)

        second = MonitoringEngine(
            config=self.config,
            cache=ContentStore(os.path.join(self.tmp, "cache")),
            snapshots=SnapshotStore(os.path.join(self.tmp, "snapshots")),
            alerts=AlertStore(os.path.join(self.tmp, "alerts_second")),
            guard=ResourceGuard(ResourceLimits()),
        )
        second_reports = [second.evaluate_batch(batch).to_dict() for batch in stream]
        self.assertEqual(second.cache.hits, len(self.config.feature_names()) * 6)
        def strip_status(reports):
            return [
                {
                    key: value
                    for key, value in report.items()
                    if key not in ("reused_features", "recomputed_features")
                }
                for report in reports
            ]

        self.assertEqual(
            canonicalize(strip_status(first_reports)),
            canonicalize(strip_status(second_reports)),
        )
        sample_key = first_reports[-1]["cache_keys"]["income_score"]
        replayed = second.cache.replay(sample_key)
        self.assertEqual(replayed["cache_key"], sample_key)

    def test_invalid_cache_key(self):
        store = ContentStore(os.path.join(self.tmp, "bad-cache"))
        with self.assertRaises(DriftError) as ctx:
            store.replay("not-a-key")
        self.assertEqual(ctx.exception.code, "E_CACHE_KEY_INVALID")

    def test_drift_triggers_alert_with_window_trace(self):
        engine = self.engine()
        normal = generate_stream(batch_count=6, sample_count=150)
        for batch in normal:
            engine.evaluate_batch(batch)
        drifted = generate_batch(
            batch_id=6,
            sample_count=150,
            injection=DriftInjection(
                batch_id=6, mean_shift=20.0, variance_multiplier=3.0, category_shift=True
            ),
        )
        report = engine.evaluate_batch(drifted)
        self.assertTrue(any(item.triggered for item in report.results))
        alert = engine.alerts.all_alerts()[0]
        self.assertEqual(alert["window_start"], report.window_start)
        self.assertEqual(alert["window_end"], report.window_end)
        self.assertEqual(
            alert["config_snapshot_version"], engine.snapshot["snapshot_version"]
        )


class InvalidationTests(EngineTestBase):
    def test_disable_feature_propagation_and_full_rebuild_equivalence(self):
        engine = self.engine()
        stream = generate_stream(batch_count=6, sample_count=100)
        reports = [engine.evaluate_batch(batch).to_dict() for batch in stream]

        new_features = dict(self.config.features)
        new_features["region"] = replace(new_features["region"], enabled=False)
        new_config = replace(self.config, features=new_features)
        propagation = engine.change_config(new_config)
        self.assertIn("region", propagation["recompute_features"])
        self.assertNotIn("region", propagation["reused_features"])
        self.assertIn("income_score", propagation["reused_features"])

        rebuild_reports = engine.rebuild_from_scratch(stream)
        # The rebuilt engine uses the new config; compare the surviving feature
        # results against the matching features of the original run.
        for original, rebuilt in zip(reports, rebuild_reports):
            original_map = {
                item["feature"]: item for item in original["feature_results"]
            }
            rebuilt_map = {
                item["feature"]: item for item in rebuilt.to_dict()["feature_results"]
            }
            self.assertNotIn("region", rebuilt_map)
            for feature, item in rebuilt_map.items():
                self.assertEqual(
                    canonicalize(item), canonicalize(original_map[feature])
                )

    def test_retype_feature_is_impact_analyzed(self):
        engine = self.engine()
        for batch in generate_stream(batch_count=5, sample_count=80):
            engine.evaluate_batch(batch)
        new_features = dict(self.config.features)
        spec = new_features["income_score"]
        new_features["income_score"] = replace(
            spec, column_type="categorical", methods=["proportion_dist"], quantiles=[]
        )
        new_config = replace(self.config, features=new_features)
        propagation = engine.change_config(new_config)
        self.assertIn("income_score", propagation["recompute_features"])


class GovernanceTests(EngineTestBase):
    def test_snapshot_rollback_rejected(self):
        engine = self.engine()
        with self.assertRaises(DriftError) as ctx:
            engine.snapshots.attempt_rollback(1)
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_ROLLBACK")

    def test_alert_becomes_stale_after_config_change(self):
        engine = self.engine()
        for batch in generate_stream(batch_count=6, sample_count=120):
            engine.evaluate_batch(batch)
        drifted = generate_batch(
            batch_id=6,
            sample_count=120,
            injection=DriftInjection(batch_id=6, mean_shift=25.0, variance_multiplier=4.0),
        )
        engine.evaluate_batch(drifted)
        alert = engine.alerts.all_alerts()[0]

        changed = replace(
            self.config,
            features={
                name: replace(spec, threshold=spec.threshold + 0.5)
                for name, spec in self.config.features.items()
            },
        )
        engine.change_config(changed)
        with self.assertRaises(DriftError) as ctx:
            engine.alerts.assert_fresh(
                alert,
                current_snapshot=engine.snapshot,
                current_cache_key="different-key",
                current_window=[alert["window_start"], alert["window_end"]],
            )
        self.assertEqual(ctx.exception.code, "E_ALERT_STALE")

    def test_resource_limit_on_batch_size(self):
        limits = ResourceLimits(max_batch_samples=10)
        engine = self.engine(limits)
        with self.assertRaises(DriftError) as ctx:
            engine.evaluate_batch(generate_batch(batch_id=0, sample_count=50))
        self.assertEqual(ctx.exception.code, "E_RESOURCE_LIMIT")

    def test_cache_entry_limit(self):
        store = ContentStore(os.path.join(self.tmp, "tiny-cache"), max_entries=1)
        from drift_monitor.cache import CacheKeyComponents

        components = CacheKeyComponents(
            window_sample_hash="a" * 64,
            methods=["mean_shift"],
            thresholds={"f": 1.0},
            stat_schema_version="stat-caliber-v1",
            feature_definition_digest="b" * 64,
            upstream_hash="c" * 64,
        )
        from drift_monitor.cache import build_cache_key

        first_key = build_cache_key(components)
        store.put(first_key, components, {"x": 1})
        second_components = replace(components, upstream_hash="d" * 64)
        second_key = build_cache_key(second_components)
        with self.assertRaises(DriftError) as ctx:
            store.put(second_key, second_components, {"x": 2})
        self.assertEqual(ctx.exception.code, "E_RESOURCE_LIMIT")


if __name__ == "__main__":
    unittest.main()
