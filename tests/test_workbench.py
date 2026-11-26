from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delivery_workbench.canon import canonical_dumps  # noqa: E402
from delivery_workbench.engine import Engine, canonical_diagnostic_report  # noqa: E402
from delivery_workbench.errors import Code, WorkbenchError  # noqa: E402
from delivery_workbench.loader import load_bundle  # noqa: E402
from delivery_workbench.scheduler import schedule_diagnostics  # noqa: E402
from delivery_workbench.semantics import merge_snapshots  # noqa: E402
from delivery_workbench.storage import Storage  # noqa: E402
from seed_samples import generate  # noqa: E402


class WorkbenchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="wb-test-", dir=str(ROOT / "state")))
        self.samples = generate(self.temp_dir / "samples")
        self.workbench = ROOT / "workbench"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def engine(self, name: str = "state.sqlite3") -> Engine:
        return Engine(self.samples, self.temp_dir / name)

    def test_load_merge_and_watermarks(self) -> None:
        bundle = load_bundle(self.samples)
        self.assertEqual(14, len(bundle["events"]))
        view = merge_snapshots(bundle, 5000)
        present = [entry for entry in view["entries"] if entry["present"]]
        self.assertEqual(13, len(present))
        self.assertEqual(["inventory/1/1"], view["missing_keys"])
        report = canonical_diagnostic_report(bundle)
        watermarks = {item["partition_key"]: item for item in report["partition_watermarks"]}
        self.assertEqual(2, watermarks["orders/0"]["watermark_offset"])
        self.assertEqual(1, watermarks["orders/1"]["watermark_offset"])
        self.assertEqual(1, watermarks["orders/2"]["watermark_offset"])
        self.assertEqual(2, watermarks["payments/0"]["watermark_offset"])
        self.assertEqual(2, watermarks["payments/1"]["watermark_offset"])
        self.assertEqual(2, watermarks["inventory/0"]["watermark_offset"])
        self.assertEqual(1, watermarks["inventory/1"]["watermark_offset"])
        self.assertEqual("WB_WM_TOMBSTONE", watermarks["orders/1"]["blocker"]["code"])

    def test_validation_error_codes_do_not_create_database(self) -> None:
        cases = {
            "duplicate_topic": Code.TOPIC_DUPLICATE,
            "bad_sequence": Code.EVENT_SEQ,
            "negative_clock": Code.EVENT_FIELD,
            "duplicate_subscriber": Code.SUBSCRIBER_DUPLICATE,
            "negative_v1_seconds": Code.V1_TIME,
            "v2_hash": Code.V2_HASH,
        }
        for case, expected_code in cases.items():
            with self.subTest(case=case):
                bad = self.temp_dir / f"bad-{case}"
                shutil.copytree(self.samples, bad)
                if case == "duplicate_topic":
                    path = bad / "topics.json"
                    data = json.loads(path.read_text())
                    data["topics"].append(dict(data["topics"][0]))
                    path.write_text(json.dumps(data))
                elif case == "bad_sequence":
                    path = bad / "events.jsonl"
                    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
                    rows[1]["seq"] = rows[0]["seq"]
                    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                elif case == "negative_clock":
                    path = bad / "events.jsonl"
                    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
                    rows[2]["logical_clock_ms"] = -1
                    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                elif case == "duplicate_subscriber":
                    path = bad / "subscriptions.json"
                    data = json.loads(path.read_text())
                    data["subscriptions"].append(dict(data["subscriptions"][0]))
                    path.write_text(json.dumps(data))
                elif case == "negative_v1_seconds":
                    path = bad / "semantics" / "v1_snapshot.json"
                    data = json.loads(path.read_text())
                    data["entries"][0]["effective_rel_sec"] = -2
                    path.write_text(json.dumps(data))
                elif case == "v2_hash":
                    target = bad / "semantics" / "v2" / "shard-000.json"
                    data = json.loads(target.read_text())
                    data["entries"][0]["semantic_generation"] += 1
                    target.write_text(json.dumps(data))
                db = bad / "should-not-exist.sqlite3"
                engine = Engine(bad, db)
                with self.assertRaises(WorkbenchError) as ctx:
                    engine.load()
                self.assertEqual(expected_code, ctx.exception.code)
                self.assertFalse(db.exists())

    def test_patch_sequence_is_replayed_identically(self) -> None:
        engine = self.engine("patch.sqlite3")
        engine.load()
        first = engine.patch()
        self.assertFalse(first["replayed"])
        second = engine.patch()
        self.assertTrue(second["replayed"])
        self.assertEqual(
            canonical_dumps(first["patches"]),
            canonical_dumps(second["patches"]),
        )
        status = engine.status()
        self.assertEqual(len(first["patches"]), len(status["patches"]))
        self.assertTrue(all("trigger_semantic_version" in patch for patch in status["patches"]))
        self.assertTrue(all(patch["upstream_offsets"] is not None for patch in status["patches"]))
        self.assertTrue(all(patch["impact_path"] for patch in status["patches"]))

    def test_scheduler_distinguishes_budget_and_concurrency(self) -> None:
        bundle = load_bundle(self.samples)
        report = canonical_diagnostic_report(bundle)
        records = {item["partition_key"]: item for item in report["partition_watermarks"]}
        budget_run = schedule_diagnostics(bundle, records, 8000, 250, 8)
        self.assertEqual(1, budget_run["counts"]["budget_unstarted"])
        self.assertGreaterEqual(budget_run["counts"]["voluntarily_abandoned"], 1)
        cap_run = schedule_diagnostics(bundle, records, 8000, 100_000, 1)
        self.assertEqual(3, cap_run["counts"]["voluntarily_abandoned"])
        self.assertEqual(0, cap_run["counts"]["budget_unstarted"])
        self.assertTrue(all(task["non_preemptive"] for task in cap_run["tasks"] if task["lifecycle"] == "started"))

    def test_incremental_package_keeps_unaffected_partition_bitwise(self) -> None:
        engine = self.engine("package.sqlite3")
        engine.load()
        engine.diagnose()
        before = {item["partition_key"]: item["content_hash"] for item in engine.status()["partition_watermarks"]}
        package = self.samples / "packages" / "pkg-002-invalidation"
        merge_result = engine.merge(package_path=package)
        self.assertEqual("append", merge_result["binding"]["decision"]["decision"])
        self.assertEqual("incremental", merge_result["binding"]["decision"]["mode"])
        result = engine.diagnose(as_of_clock_ms=9000)
        self.assertIn("inventory/0", result["watermark_recompute"]["reused_partitions"])
        after = {item["partition_key"]: item["content_hash"] for item in engine.status()["partition_watermarks"]}
        self.assertEqual(before["inventory/0"], after["inventory/0"])
        changed = {item["partition_key"]: item for item in result["watermarks"]}
        self.assertEqual(0, changed["payments/0"]["watermark_offset"])
        self.assertEqual(1, changed["payments/1"]["watermark_offset"])
        self.assertEqual(2, changed["inventory/1"]["watermark_offset"])
        self.assertEqual(2, changed["orders/2"]["watermark_offset"])
        with self.assertRaises(WorkbenchError) as ctx:
            engine.merge(package_path=self.samples / "packages" / "pkg-009-rollback")
        self.assertEqual(Code.VERSION_ROLLBACK, ctx.exception.code)
        full_result = engine.merge(package_path=self.samples / "packages" / "pkg-003-full-replacement")
        self.assertEqual("full", full_result["binding"]["decision"]["mode"])
        full_diagnosis = engine.diagnose(as_of_clock_ms=9000)
        full_watermarks = {item["partition_key"]: item for item in full_diagnosis["watermarks"]}
        self.assertEqual(0, full_watermarks["payments/0"]["watermark_offset"])
        self.assertEqual(2, full_watermarks["orders/2"]["watermark_offset"])

    def test_sigkill_prepared_checkpoint_resumes_to_verified_state(self) -> None:
        db = self.temp_dir / "crash.sqlite3"
        base = [sys.executable, str(self.workbench), "--sample-dir", str(self.samples), "--db", str(db)]
        loaded = subprocess.run(base + ["load"], text=True, capture_output=True)
        self.assertEqual(0, loaded.returncode, loaded.stdout + loaded.stderr)
        env = dict(os.environ, WB_CRASH_POINT="checkpoint_after_prepare")
        crashed = subprocess.run(base + ["diagnose"], text=True, capture_output=True, env=env)
        self.assertNotEqual(0, crashed.returncode)
        storage = Storage(db)
        self.assertEqual(1, len(storage.prepared_checkpoint_ids()))
        resumed = subprocess.run(base + ["resume"], text=True, capture_output=True)
        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        verified = subprocess.run(base + ["verify"], text=True, capture_output=True)
        self.assertEqual(0, verified.returncode, verified.stdout + verified.stderr)
        payload = json.loads(verified.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(0, payload["result"]["field_diff_count"])
        self.assertTrue(payload["result"]["current_database"]["ok"])

    def test_concurrent_distinct_subscribers_commit_once(self) -> None:
        db = self.temp_dir / "concurrent.sqlite3"
        subprocess.run(
            [sys.executable, str(self.workbench), "--sample-dir", str(self.samples), "--db", str(db), "load"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        subscribers = ["sub-orders-a", "sub-orders-b", "sub-payments", "sub-inventory"]
        processes = []
        for subscriber in subscribers:
            processes.append(
                subprocess.Popen(
                    [sys.executable, str(self.workbench), "--sample-dir", str(self.samples), "--db", str(db), "diagnose", "--subscriber-id", subscriber],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(0, process.returncode, stdout + stderr)
        import sqlite3

        conn = sqlite3.connect(db)
        try:
            self.assertEqual(7, conn.execute("SELECT COUNT(*) FROM partition_commit_ledger").fetchone()[0])
            self.assertEqual(4, conn.execute("SELECT COUNT(*) FROM cursor_commit_ledger").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM subscription_cursors WHERE cursor_revision <> 1").fetchone()[0])
            self.assertEqual(4, conn.execute("SELECT COUNT(*) FROM subscription_cursors").fetchone()[0])
        finally:
            conn.close()

    def test_command_orders_produce_bitwise_same_status(self) -> None:
        db1 = self.temp_dir / "order1.sqlite3"
        e1 = Engine(self.samples, db1)
        e1.load(); e1.merge(); e1.diagnose()
        s1 = e1.status()

        db2 = self.temp_dir / "order2.sqlite3"
        e2 = Engine(self.samples, db2)
        e2.load(); e2.patch(); e2.diagnose()
        s2 = e2.status()

        db3 = self.temp_dir / "order3.sqlite3"
        e3 = Engine(self.samples, db3)
        e3.load(); e3.diagnose(); e3.merge(); e3.patch()
        s3 = e3.status()
        self.assertEqual(canonical_dumps(s1), canonical_dumps(s2))
        self.assertEqual(canonical_dumps(s1), canonical_dumps(s3))


if __name__ == "__main__":
    unittest.main()
