"""End-to-end and unit tests for the deterministic recall/fusion system."""

from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from recall_fusion.budget import (
    BudgetLedger,
    estimate_query_bytes,
    plan_query,
)
from recall_fusion.config import (
    CHANNEL_ORDER,
    CHANNEL_VECTOR,
    DEDUP_FIRST_HIT,
    DEFAULT_CONFIG,
    NORM_RANK_INV,
    Config,
    load_config,
)
from recall_fusion.data import DataStore, generate_dataset
from recall_fusion.engine import Engine
from recall_fusion.errors import (
    BusinessError,
    E_CONFIG_INVALID,
    E_RECALL_BUDGET,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
)
from recall_fusion.fusion import run_fusion
from recall_fusion.recall import run_channel
from recall_fusion.snapshots import SnapshotStore, checksum_payload


def small_config(**overrides) -> Config:
    base = DEFAULT_CONFIG.to_dict()
    base["datagen"]["num_docs"] = 80
    base["datagen"]["num_queries"] = 12
    base["datagen"]["num_terms"] = 25
    base["datagen"]["vector_dim"] = 16
    base["workers"] = 1
    base["batch_size"] = 4
    base["total_candidate_budget"] = 10_000
    base["fusion_compare_budget"] = 10_000
    base["memory_limit_bytes"] = 10_000_000
    base.update(overrides)
    return load_config_from_dict(base)


def load_config_from_dict(raw: dict) -> Config:
    # Reuse the validated builder through a temp JSON file.
    import recall_fusion.config as cfg_mod

    return cfg_mod._build_config(raw)


def build_store(config: Config) -> tuple[DataStore, list]:
    docs, queries = generate_dataset(config.datagen)
    return DataStore(docs, config.datagen, config.precision), queries


class DataAndRecallTests(unittest.TestCase):
    def setUp(self):
        self.config = small_config()
        self.store, self.queries = build_store(self.config)

    def test_dataset_is_deterministic(self):
        docs_a, q_a = generate_dataset(self.config.datagen)
        docs_b, q_b = generate_dataset(self.config.datagen)
        self.assertEqual([d.__dict__ for d in docs_a], [d.__dict__ for d in docs_b])
        self.assertEqual([q.__dict__ for q in q_a], [q.__dict__ for q in q_b])

    def test_vectors_are_process_independent(self):
        from recall_fusion.data import vector_for

        v1 = vector_for("d000001", 16, self.config.datagen.seed, "fp64")
        v2 = vector_for("d000001", 16, self.config.datagen.seed, "fp64")
        self.assertTrue(np.array_equal(v1, v2))

    def test_four_channels_reproducible_and_marked(self):
        query = self.queries[0]
        for channel in CHANNEL_ORDER:
            limit = self.config.channels[channel].candidate_limit
            out_a = run_channel(channel, self.store, query, limit)
            out_b = run_channel(channel, self.store, query, limit)
            self.assertEqual(out_a.to_dict(), out_b.to_dict())
            self.assertLessEqual(len(out_a.candidates), limit)
            if out_a.truncated:
                self.assertGreater(out_a.matched_total, len(out_a.candidates))
            # score-descending ordering, with ascending doc_id on exact ties
            for prev, nxt in zip(out_a.candidates, out_a.candidates[1:]):
                self.assertGreaterEqual(prev.raw_score, nxt.raw_score)
                if prev.raw_score == nxt.raw_score:
                    self.assertLess(prev.doc_id, nxt.doc_id)


class FusionTests(unittest.TestCase):
    def setUp(self):
        self.config = small_config()
        self.store, self.queries = build_store(self.config)

    def _outputs(self, query, limits=None):
        limits = limits or {
            c: self.config.channels[c].candidate_limit for c in CHANNEL_ORDER
        }
        return {c: run_channel(c, self.store, query, limits[c]) for c in CHANNEL_ORDER}

    def test_recomputable_scores_and_explicit_empty_channels(self):
        outputs = self._outputs(self.queries[0])
        fused, _, _ = run_fusion(outputs, self.config, two_stage=False)
        self.assertGreater(len(fused), 0)
        for candidate in fused:
            recomputed = sum(row["contribution"] for row in candidate.details)
            self.assertTrue(math.isclose(recomputed, candidate.fused_score, abs_tol=1e-12))
            for row in candidate.details:
                if row["hit"]:
                    self.assertTrue(
                        math.isclose(
                            row["contribution"], row["weight"] * row["norm_score"]
                        )
                    )
                else:
                    self.assertIsNone(row["raw_score"])
                    self.assertIsNone(row["norm_score"])
        ids = [c.doc_id for c in fused]
        self.assertEqual(len(ids), len(set(ids)))

    def test_first_hit_policy_only_counts_first_channel(self):
        cfg = load_config_from_dict(
            {**self.config.to_dict(), "dedup_policy": DEDUP_FIRST_HIT}
        )
        outputs = self._outputs(self.queries[1])
        fused, _, _ = run_fusion(outputs, cfg, two_stage=False)
        multi = next(c for c in fused if len(c.hit_channels) > 1)
        active = [row for row in multi.details if row["contribution"] > 0]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["channel"], multi.primary_channel)

    def test_rank_inverse_normalization(self):
        outputs = self._outputs(self.queries[0])
        cfg = load_config_from_dict(
            {**self.config.to_dict(), "normalization": NORM_RANK_INV}
        )
        fused, _, _ = run_fusion(outputs, cfg, two_stage=False)
        self.assertTrue(all(c.fused_score >= 0 for c in fused))

    def test_two_stage_consumes_extra_comparisons(self):
        outputs = self._outputs(self.queries[2])
        single, single_cmp, single_mode = run_fusion(
            outputs, self.config, two_stage=False
        )
        two, two_cmp, two_mode = run_fusion(outputs, self.config, two_stage=True)
        self.assertEqual(single_mode, "single_stage")
        if two_mode == "two_stage":
            self.assertGreater(two_cmp, single_cmp)
            head = two[0]
            self.assertIn("coarse_norm_score", head.details[0])

    def test_invalid_normalization_raises(self):
        raw = self.config.to_dict()
        raw["normalization"] = "bogus"
        with self.assertRaises(BusinessError) as ctx:
            load_config_from_dict(raw)
        self.assertEqual(ctx.exception.code, E_CONFIG_INVALID)
        self.assertEqual(ctx.exception.field, "normalization")

    def test_negative_weight_raises_with_field(self):
        raw = self.config.to_dict()
        raw["channels"][CHANNEL_VECTOR]["weight"] = -0.1
        with self.assertRaises(BusinessError) as ctx:
            load_config_from_dict(raw)
        self.assertEqual(ctx.exception.code, E_CONFIG_INVALID)
        self.assertIn("vector", ctx.exception.field)


class BudgetAndPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = small_config()
        self.store, self.queries = build_store(self.config)

    def test_degradation_is_recorded_and_reproducible(self):
        ledger = BudgetLedger(
            total_candidate_budget=60,
            fusion_compare_budget=10_000,
            memory_limit_bytes=10_000_000,
        )
        plan_a = plan_query(self.store, self.queries[0], self.config, ledger)
        plan_b = plan_query(
            self.store, self.queries[0], self.config,
            BudgetLedger(60, 10_000, 10_000_000),
        )
        self.assertEqual(plan_a.to_dict(), plan_b.to_dict())
        self.assertTrue(plan_a.degradation_steps)
        self.assertLess(
            sum(plan_a.effective_limits.values()),
            sum(plan_a.requested_limits.values()),
        )

    def test_rejection_returns_recall_budget(self):
        ledger = BudgetLedger(
            total_candidate_budget=0,
            fusion_compare_budget=0,
            memory_limit_bytes=10_000_000,
        )
        plan = plan_query(self.store, self.queries[0], self.config, ledger)
        self.assertFalse(plan.admitted)
        self.assertEqual(plan.error_code, E_RECALL_BUDGET)
        self.assertTrue(plan.reason)

    def test_memory_limit_returns_resource_limit(self):
        plan = plan_query(
            self.store,
            self.queries[0],
            self.config,
            BudgetLedger(10_000, 10_000, memory_limit_bytes=1),
        )
        self.assertFalse(plan.admitted)
        self.assertEqual(plan.error_code, E_RESOURCE_LIMIT)

    def test_estimate_bytes_positive(self):
        size = estimate_query_bytes(50, 40, self.config)
        self.assertGreater(size, 0)


class SnapshotTests(unittest.TestCase):
    def test_replace_rollback_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(tmp)
            store.submit("fusion_weights", 1, {"a": 1})
            store.submit("fusion_weights", 2, {"a": 2})
            self.assertEqual(store.current_version("fusion_weights"), 2)
            loaded = store.load("fusion_weights")
            self.assertEqual(loaded.payload, {"a": 2})
            with self.assertRaises(BusinessError) as rb:
                store.submit("fusion_weights", 1, {"a": 0})
            self.assertEqual(rb.exception.code, E_SNAPSHOT_ROLLBACK)

            path = store._version_path("fusion_weights", 2)
            record = json.loads(path.read_text(encoding="utf-8"))
            record["payload"] = {"a": 999}
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(BusinessError) as cp:
                store.load("fusion_weights", 2)
            self.assertEqual(cp.exception.code, E_SNAPSHOT_CORRUPT)
            self.assertIn("fusion_weights", cp.exception.field)

    def test_checksum_detects_content_change(self):
        self.assertNotEqual(checksum_payload({"a": 1}), checksum_payload({"a": 2}))


class EngineIntegrationTests(unittest.TestCase):
    def test_full_run_and_rerun_byte_identical(self):
        config = small_config(memory_limit_bytes=40_000)
        with tempfile.TemporaryDirectory() as tmp:
            out_a = Path(tmp) / "a"
            out_b = Path(tmp) / "b"
            summary_a = Engine(config, out_a).run()
            Engine(config, out_b).run()
            hash_a = (out_a / "results.canonical.jsonl").read_bytes()
            hash_b = (out_b / "results.canonical.jsonl").read_bytes()
            self.assertEqual(hash_a, hash_b)
            self.assertEqual(summary_a["queries_failed"], 0)
            # memory eviction happened and ledger captured every event
            self.assertGreaterEqual(summary_a["memory"]["eviction_count"], 1)
            ledger_lines = (out_a / "cache" / "memory_ledger.jsonl").read_text()
            self.assertIn("evict", ledger_lines)
            # every result row has all four channels recorded
            for line in (out_a / "results.jsonl").read_text().splitlines():
                row = json.loads(line)
                self.assertEqual(
                    sorted(row["channel_outputs"].keys()), sorted(CHANNEL_ORDER)
                )

    def test_budget_pressure_produces_rejections(self):
        config = small_config(
            total_candidate_budget=10_000,
            fusion_compare_budget=10_000,
            memory_limit_bytes=128,
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary = Engine(config, Path(tmp) / "run").run()
            self.assertGreater(summary["queries_rejected"], 0)
            rejected = [
                json.loads(line)
                for line in (Path(tmp) / "run" / "cache" / "rejected.jsonl").read_text().splitlines()
            ]
            self.assertTrue(
                all(
                    r["error_code"] in (E_RECALL_BUDGET, E_RESOURCE_LIMIT)
                    for r in rejected
                )
            )

    def test_worker_failure_is_isolated(self):
        config = small_config()
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(config, Path(tmp) / "run")
            store, queries = engine.prepare_dataset()

            from recall_fusion.budget import plan_query
            from recall_fusion import worker as worker_mod
            from recall_fusion.worker import _failure

            self.addCleanup(engine.memory.close)
            tasks = []
            for query in queries:
                plan = plan_query(store, query, config, engine.ledger)
                if plan.admitted:
                    tasks.append((query, plan.to_dict()))

            real_process = worker_mod.process_query

            def boom(task):
                query, _ = task
                if query.query_id == queries[1].query_id:
                    try:
                        raise RuntimeError("synthetic worker explosion")
                    except Exception as exc:
                        return _failure(query.query_id, "process_query", exc)
                return real_process(task)

            worker_mod.init_worker(store, config)

            outcomes = [boom(task) for task in tasks]
            failures = [o for o in outcomes if not o["ok"]]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["query_id"], queries[1].query_id)
            self.assertEqual(failures[0]["error"]["stage"], "process_query")
            self.assertEqual(failures[0]["error"]["code"], "E_WORKER_FAILURE")
            self.assertTrue(all(o["ok"] for o in outcomes if o is not failures[0]))

    def test_env_override_changes_workers(self):
        os.environ["RF_WORKERS"] = "3"
        try:
            cfg = load_config(None)
            self.assertEqual(cfg.workers, 3)
        finally:
            del os.environ["RF_WORKERS"]

    def test_rerun_same_directory_advances_snapshot_versions(self):
        config = small_config()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            first = Engine(config, out).run()
            second = Engine(config, out).run()
            for kind in ("recall_stats", "norm_params", "fusion_weights"):
                self.assertEqual(first["snapshots"][kind], 1)
                self.assertEqual(second["snapshots"][kind], 2)
            # whole-snapshot replacement leaves both immutable versions on disk
            store = SnapshotStore(out / "snapshots")
            self.assertEqual(store.list_versions("fusion_weights"), [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
