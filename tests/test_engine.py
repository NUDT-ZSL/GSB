from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_tuner.canonical import canonical_dumps
from quant_tuner import sample_data
from quant_tuner.config_io import detect_and_load, load_v1, load_v2, migrate_config
from quant_tuner.engine import TuningEngine
from quant_tuner.errors import (
    E_BUDGET_EXCEEDED,
    E_FORMAT_CORRUPT,
    E_PRECISION_TRADE,
    E_STAGE_BUSY,
    E_VERSION_UNSUPPORTED,
    TuningError,
)
from quant_tuner.ledger import CommitLock, SnapshotStore
from quant_tuner.planner import plan_search
from quant_tuner.propagation import plan_propagation
from quant_tuner.recovery import recover_workspace
from quant_tuner.sample_data import engine_config, platform_stats, tuning_tasks
from quant_tuner.self_check import self_check, verify_propagation_document
from quant_tuner.workload import compute_quant_report


class QuantTests(unittest.TestCase):
    def test_metrics_are_byte_deterministic(self) -> None:
        kwargs = {
            "tuning_id": "mm-0001",
            "op_kind": "matmul",
            "workload_shape": {"M": 16, "N": 24, "K": 32},
            "quant_config": {"scale": 0.04, "zero_point": 0, "bits": 8},
        }
        first = compute_quant_report(**kwargs)
        second = compute_quant_report(**kwargs)
        self.assertEqual(first["metrics_digest"], second["metrics_digest"])
        self.assertEqual(canonical_dumps(first), canonical_dumps(second))
        self.assertEqual(
            first["quantized_array_digests"], second["quantized_array_digests"]
        )

    def test_uint8_zero_point_domain(self) -> None:
        report = compute_quant_report(
            "conv-x",
            "conv2d",
            sample_data.tuning_tasks()[2]["workload_shape"],
            {"scale": 0.04, "zero_point": 128, "bits": 8, "dtype": "uint8"},
        )
        self.assertEqual(report["metrics"]["clipped_values"], 0)
        self.assertLess(report["metrics"]["relative_rmse"], 0.08)


class ConfigMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = sample_data.generate_all(self.root / "samples")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_v1_v2_unified_roundtrip(self) -> None:
        v1 = load_v1(self.paths["v1"])
        v2 = load_v2(self.paths["v2"])
        self.assertEqual(len(v1["records"]), len(v2["records"]))
        self.assertEqual(
            v1["records"][0]["quant_config"]["scale"],
            v2["records"][0]["quant_config"]["scale"],
        )
        output = self.root / "from-v2-v1.json"
        migrate_config(self.paths["v2"], "v1", output)
        migrated = load_v1(output)
        raw_v1 = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(raw_v1["revisions"]), len(v2["records"]))
        self.assertEqual(len(migrated["records"]), len(v2["records"]))

    def test_unknown_version(self) -> None:
        path = self.root / "unknown.json"
        path.write_text(json.dumps({"version": "v9"}), encoding="utf-8")
        with self.assertRaises(TuningError) as ctx:
            detect_and_load(path)
        self.assertEqual(ctx.exception.code, E_VERSION_UNSUPPORTED)

    def test_v1_missing_and_timestamp_regression(self) -> None:
        missing = self.root / "missing.json"
        missing.write_text(
            json.dumps({"version": "v1", "origin_absolute_ms": 0, "revisions": [{}]}),
            encoding="utf-8",
        )
        with self.assertRaises(TuningError) as ctx:
            load_v1(missing)
        self.assertEqual(ctx.exception.code, E_FORMAT_CORRUPT)

        bad_time = self.root / "bad-time.json"
        bad_time.write_text(
            json.dumps(
                {
                    "version": "v1",
                    "origin_absolute_ms": 1000,
                    "revisions": [
                        {
                            "record_id": "a",
                            "tuning_id": "t",
                            "effective_second": 2.0,
                            "generation": 1,
                            "quant_scale": 0.1,
                            "quant_zero_point": 0,
                            "schedule_id": "s",
                        },
                        {
                            "record_id": "b",
                            "tuning_id": "t",
                            "effective_second": 1.0,
                            "generation": 1,
                            "quant_scale": 0.1,
                            "quant_zero_point": 0,
                            "schedule_id": "s2",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(TuningError) as ctx:
            load_v1(bad_time)
        self.assertEqual(ctx.exception.code, E_FORMAT_CORRUPT)

    def test_v2_timestamp_regression(self) -> None:
        db = Path(self.paths["v2"]) / "meta.sqlite3"
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT record_id, absolute_ms FROM config_meta ORDER BY rowid LIMIT 1 OFFSET 1"
            ).fetchone()
            conn.execute(
                "UPDATE config_meta SET absolute_ms = ? WHERE record_id = ?",
                (row[1] - 10_000_000, row[0]),
            )
            conn.commit()
        with self.assertRaises(TuningError) as ctx:
            load_v2(self.paths["v2"])
        self.assertEqual(ctx.exception.code, E_FORMAT_CORRUPT)


class PlannerArbitrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = tuning_tasks()
        self.platform = platform_stats()
        self.policy = engine_config()["arbitration"]
        self.policy.update(
            {
                "timing_repeats": 1,
                "warmup_repeats": 0,
                "worker_count": 3,
                "max_concurrency": 3,
            }
        )

    def test_progress_priority_selects_max_concurrency(self) -> None:
        plan = plan_search(self.tasks, self.platform, self.policy)
        self.assertEqual(plan["selected"]["concurrency"], 3)
        self.assertEqual(len(plan["rejections"]), 0)

    def test_memory_priority_selects_concurrency_one(self) -> None:
        policy = dict(self.policy)
        policy["priority_order"] = ["memory", "progress", "precision"]
        plan = plan_search(self.tasks, self.platform, policy)
        self.assertEqual(plan["selected"]["concurrency"], 1)

    def test_precision_priority_selects_max_precision(self) -> None:
        policy = dict(self.policy)
        policy["priority_order"] = ["precision", "progress", "memory"]
        plan = plan_search(self.tasks, self.platform, policy)
        self.assertEqual(plan["selected"]["precision_mode"], "max")

    def test_unachievable_precision_returns_code(self) -> None:
        tasks = [self.tasks[0]]
        tasks[0]["quant_config"] = {"bits": 8, "scale": 1e-7, "zero_point": 0}
        policy = dict(self.policy)
        policy["max_relative_rmse"] = 1e-12
        plan = plan_search(tasks, self.platform, policy)
        self.assertTrue(any(item["code"] == E_PRECISION_TRADE for item in plan["rejections"]))

    def test_precision_upgrade_can_hit_dequant_budget(self) -> None:
        tasks = [self.tasks[0]]
        tasks[0]["workload_shape"] = {"M": 8, "N": 8, "K": 8}
        tasks[0]["quant_config"] = {
            "bits": 8,
            "dtype": "int8",
            "scale": 0.001,
            "zero_point": 0,
        }
        policy = dict(self.policy)
        policy["max_relative_rmse"] = 0.01
        policy["max_dequant_ops"] = 10
        plan = plan_search(tasks, self.platform, policy)
        self.assertTrue(any(item["code"] == E_BUDGET_EXCEEDED for item in plan["rejections"]))

    def test_memory_budget_returns_budget_code(self) -> None:
        policy = dict(self.policy)
        policy["memory_budget_mb"] = 1.0
        plan = plan_search(self.tasks[:1], self.platform, policy)
        self.assertTrue(any(item["code"] == E_BUDGET_EXCEEDED for item in plan["rejections"]))


class LedgerRecoveryTests(unittest.TestCase):
    def test_nonblocking_commit_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "commit.lock"
            first = CommitLock(lock_path).acquire()
            with self.assertRaises(TuningError) as ctx:
                CommitLock(lock_path).acquire()
            self.assertEqual(ctx.exception.code, E_STAGE_BUSY)
            first.release()

    def test_kill_recovery_keeps_committed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "run"
            tasks = tuning_tasks()[:2]
            platform = platform_stats()
            policy = dict(engine_config()["arbitration"])
            policy.update(
                {
                    "worker_count": 2,
                    "max_concurrency": 2,
                    "timing_repeats": 1,
                    "warmup_repeats": 0,
                }
            )
            first_engine = TuningEngine(output, tasks, platform, policy)
            first_engine.run(stop_after_batches=1)
            store = SnapshotStore(output)
            self.assertEqual(store.latest_version(), 1)
            before = store.load_latest()["payload"]
            before_ns = {
                item["point_id"]: item["measured"]["elapsed_ns_median"]
                for item in before["results"]
            }

            stale_partial = store.staging_dir / "uncommitted.partial.json"
            stale_partial.write_text("{}", encoding="utf-8")
            inflight = store.work_dir / "batch-000002.inflight"
            inflight.write_text("stale", encoding="utf-8")

            recovery = recover_workspace(output)
            self.assertEqual(recovery["checkpoint_version"], 1)
            self.assertIn("staging/uncommitted.partial.json", recovery["discarded_uncommitted_files"])
            self.assertIn("work/batch-000002.inflight", recovery["discarded_uncommitted_files"])
            self.assertFalse(stale_partial.exists())
            self.assertFalse(inflight.exists())

            engine = TuningEngine(output, tasks, platform, policy)
            report = engine.run(resume=True)
            self.assertTrue(report["complete"])
            self.assertEqual(report["checkpoint_version"], 3)

            after = store.load_latest()["payload"]
            after_ns = {
                item["point_id"]: item["measured"]["elapsed_ns_median"]
                for item in after["results"]
            }
            for point_id, elapsed_ns in before_ns.items():
                self.assertEqual(after_ns[point_id], elapsed_ns)

            check = self_check(output, tasks, platform, policy)
            self.assertEqual(check["committed_points"], 6)


class PropagationTests(unittest.TestCase):
    def test_sets_partition_and_covers_changes(self) -> None:
        old = tuning_tasks()
        new = sample_data.revised_tasks(old)
        document = plan_propagation(old, new)
        summary = verify_propagation_document(document)
        self.assertEqual(len(document["recompute_points"]), 4)
        self.assertIn("mm-0001", document["quant_changed"])
        self.assertTrue(
            any(item[0] == "conv-0001" for item in document["schedule_changed"])
        )
        self.assertGreaterEqual(summary["recompute_count"], 4)


if __name__ == "__main__":
    unittest.main()
