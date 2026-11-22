from __future__ import annotations

import unittest

from hotgray.dependency import analyze_resource_change
from hotgray.errors import BizError, ErrorCode
from hotgray.shards import compare_target_directories, compose_patch_shards

from .support import (
    SAMPLES,
    TempWorkspace,
    copy_samples,
    mutate_json,
    new_engine,
    sqlite_rows,
)


class PatchAndInvalidationTests(unittest.TestCase):
    def test_patch_reconstructs_exact_target_chain(self) -> None:
        with TempWorkspace() as workspace:
            engine = new_engine(workspace.path)
            result = engine.apply_patch(at_ms=1000)
            target_model = engine.manifest.model(2)
            self.assertEqual(result["shard_chain"], target_model.shard_chain())
            self.assertEqual(result["comparison_count"], len(target_model.shard_chain()))
            self.assertEqual(result["invalidations"]["affected_replicas"], ["replica-a", "replica-b"])

            composed = compose_patch_shards(
                manifest=engine.manifest,
                patch=engine.patch,
                samples_root=SAMPLES,
            )
            comparisons = compare_target_directories(
                materializer=engine.materializer,
                manifest=engine.manifest,
                target_version=2,
                composed=composed,
            )
            self.assertEqual(len(comparisons), 8)
            changed = [item for item in composed if item.source == "patch"]
            self.assertEqual(
                {(item.layer, item.shard_index) for item in changed},
                {("encoder.1", 1), ("head", 0)},
            )

            shard_rows = sqlite_rows(
                engine.store.db_path,
                "SELECT version, layer, shard_index, digest FROM shards ORDER BY version, layer, shard_index",
            )
            target_rows = [row for row in shard_rows if row["version"] == 2]
            self.assertEqual(
                [(row["layer"], row["shard_index"]) for row in target_rows],
                [(item["layer"], item["shard_index"]) for item in target_model.shard_chain()],
            )

    def test_incremental_patch_reuses_unchanged_shards_and_materializes_digests_once(self) -> None:
        with TempWorkspace() as workspace:
            engine = new_engine(workspace.path)
            baseline = engine.manifest.model(1)
            target = engine.manifest.model(2)
            baseline_digests = {item["digest"] for item in baseline.shard_chain()}
            target_digests = {item["digest"] for item in target.shard_chain()}
            self.assertEqual(len(baseline_digests), 8)
            self.assertEqual(len(target_digests), 8)
            self.assertEqual(len(baseline_digests & target_digests), 6)

            engine.apply_patch(at_ms=1000)
            rows = sqlite_rows(
                engine.store.db_path,
                "SELECT digest, COUNT(*) AS count FROM materializations GROUP BY digest",
            )
            self.assertTrue(all(row["count"] == 1 for row in rows))
            self.assertEqual(len(rows), 10)
            self.assertEqual(
                sqlite_rows(engine.store.db_path, "SELECT COUNT(*) AS count FROM version_bindings WHERE version = 2 AND committed = 1")[0]["count"],
                1,
            )

    def test_invalidation_closure_marks_exact_caches_and_reuse(self) -> None:
        with TempWorkspace() as workspace:
            engine = new_engine(workspace.path)
            result = engine.apply_patch(at_ms=1000)
            invalidations = result["invalidations"]["decisions"]
            changed_nodes = {
                item["node_id"] for item in result["invalidations"]["changed"]
            }
            self.assertIn("shard:2:encoder.1:1", changed_nodes)
            self.assertIn("shard:2:head:0", changed_nodes)
            self.assertIn("resource:pre:v2", changed_nodes)
            self.assertNotIn("resource:post:v1", changed_nodes)

            load_invalid = [
                item for item in invalidations
                if item["replica_id"] == "replica-a"
                and item["cache_type"] == "load_cache"
                and item["version"] == 2
                and item["decision"] == "invalidated"
            ]
            self.assertTrue(load_invalid)
            self.assertTrue(
                all(
                    any(node.startswith("shard:2:") for node in item["path"])
                    for item in load_invalid
                )
            )
            config_paths = [
                item["path"]
                for item in invalidations
                if item["replica_id"] == "replica-a"
                and item["cache_type"] == "warmup"
                and (item.get("source") or {}).get("resource_id") == "feature:v2"
            ]
            self.assertTrue(config_paths)
            self.assertIn("model:2", config_paths[0])
            post_sources = [
                item for item in invalidations
                if (item.get("source") or {}).get("resource_id") == "post:v1"
            ]
            self.assertEqual(post_sources, [])
            baseline_reuse = [
                item for item in invalidations
                if item["replica_id"] == "replica-control"
                and item["version"] == 1
                and item["decision"] == "reused"
            ]
            self.assertEqual(len(baseline_reuse), 3)

            snapshot = engine.snapshot()
            control = next(item for item in snapshot["replicas"] if item["replica_id"] == "replica-control")
            self.assertEqual(control["invalidated"], [])
            self.assertEqual(len(control["reusable"]), 3)

    def test_config_change_analysis_invalidates_warmup_and_inference_not_load_cache(self) -> None:
        with TempWorkspace() as workspace:
            engine = new_engine(workspace.path)
            report = analyze_resource_change(
                engine.manifest,
                resource_id="post:v1",
                baseline_version=1,
                target_version=2,
                at_ms=1000,
                replica_ids=["replica-a"],
            )
            by_cache = {item.cache_type: item for item in report.decisions}
            self.assertEqual(by_cache["load_cache"].decision, "reused")
            self.assertEqual(by_cache["warmup"].decision, "invalidated")
            self.assertEqual(by_cache["inference"].decision, "invalidated")
            self.assertIn("model:2", by_cache["warmup"].path)

    def test_incomplete_patch_is_localized_and_not_committed(self) -> None:
        with TempWorkspace() as workspace:
            samples = copy_samples(workspace.path)
            patch_path = samples / "patches" / "v1_to_v2.json"
            mutate_json(
                patch_path,
                lambda value: {
                    **value,
                    "entries": [
                        entry
                        for entry in value["entries"]
                        if not (entry["layer"] == "head" and entry["shard_index"] == 0)
                    ],
                },
            )
            engine = new_engine(workspace.path, samples)
            with self.assertRaises(BizError) as raised:
                engine.apply_patch(at_ms=1000)
            error = raised.exception
            self.assertEqual(error.code, ErrorCode.SHARD_DIGEST_MISMATCH)
            self.assertEqual(error.details["version"], 2)
            self.assertEqual(error.details["layer"], "head")
            self.assertEqual(error.details["shard_index"], 0)
            rows = sqlite_rows(
                engine.store.db_path,
                "SELECT COUNT(*) AS count FROM version_bindings WHERE version = 2",
            )
            self.assertEqual(rows[0]["count"], 0)

    def test_repeated_patch_is_idempotent(self) -> None:
        with TempWorkspace() as workspace:
            engine = new_engine(workspace.path)
            first = engine.apply_patch(at_ms=1000)
            before = engine.snapshot()["materializations"]["count"]
            second = engine.apply_patch(at_ms=1010)
            after = engine.snapshot()["materializations"]["count"]
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(before, after)
            decisions = sqlite_rows(
                engine.store.db_path,
                "SELECT COUNT(*) AS count FROM invalidations",
            )[0]["count"]
            self.assertGreater(decisions, 0)
            engine.apply_patch(at_ms=1020)
            self.assertEqual(
                sqlite_rows(engine.store.db_path, "SELECT COUNT(*) AS count FROM invalidations")[0]["count"],
                decisions,
            )


if __name__ == "__main__":
    unittest.main()
