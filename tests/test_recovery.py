import os
import sqlite3
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scaler.errors import ScalerError
from tests.helpers import Harness

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.harness.load()

    def tearDown(self):
        self.harness.cleanup()

    def test_crash_leaves_pending_action_and_resume_replays_once(self):
        orch = self.harness.orchestrator()
        context = self.harness.context()
        result = orch.reconcile(context)
        round_no = result["round_no"]
        # simulate a forced kill: flip one finished action back to pending and
        # roll its durable replica watermark to the pre-round value
        target = context.services[0].name
        before = orch.storage.get_service_state(target)
        orch.storage.connection.execute(
            "UPDATE actions SET status='pending', outcome='running' "
            "WHERE round_no=? AND service=?", (round_no, target)
        )
        orch.close()

        resumed = self.harness.orchestrator()
        resume_result = resumed.resume(self.harness.context())
        replayed = [r["service"] for r in resume_result["replayed"]]
        self.assertIn(target, replayed)
        # a second resume must be a no-op: no double deduction
        again = resumed.resume(self.harness.context())
        self.assertEqual(again["replayed"], [])
        self.assertEqual(
            resumed.storage.get_service_state(target)["replicas"],
            before["replicas"],
        )
        self.assertTrue(resumed.verify(self.harness.context())["ok"])
        resumed.close()

    def test_resume_without_pending_is_idempotent(self):
        orch = self.harness.orchestrator()
        orch.reconcile(self.harness.context())
        result = orch.resume(self.harness.context())
        self.assertEqual(result["replayed"], [])
        orch.close()


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.harness.load()

    def tearDown(self):
        self.harness.cleanup()

    def test_version_rollback_rejected(self):
        orch = self.harness.orchestrator()
        with self.assertRaises(ScalerError) as ctx:
            orch.load_package(
                self.harness.context(), "1.0.0", 1, ["h-old"]
            )
        self.assertEqual(ctx.exception.code, "E_VERSION_ROLLBACK")
        orch.close()

    def test_same_version_same_hash_is_noop_and_idempotent(self):
        orch = self.harness.orchestrator()
        context, manifest = self.harness.context(), None
        from scaler.cli import build_context

        _, manifest = build_context(self.harness.sample_dir)
        hashes = [s["shard_hash"] for s in manifest["shards"]]
        first = orch.load_package(
            context, manifest["version"],
            manifest["generation_floor"], hashes,
        )
        second = orch.load_package(
            self.harness.context(), manifest["version"],
            manifest["generation_floor"], hashes,
        )
        self.assertEqual(first["mode"], "no_op")
        self.assertEqual(second["mode"], "no_op")
        orch.close()

    def test_generation_floor_rollback_rejected(self):
        orch = self.harness.orchestrator()
        with self.assertRaises(ScalerError) as ctx:
            orch.load_package(
                self.harness.context(), "3.0.0", 1, ["h-new-1", "h-new-2"]
            )
        self.assertEqual(ctx.exception.code, "E_VERSION_ROLLBACK")
        orch.close()

    def test_incremental_append_detection(self):
        orch = self.harness.orchestrator()
        _, manifest = __import__(
            "scaler.cli", fromlist=["build_context"]
        ).build_context(self.harness.sample_dir)
        existing = [s["shard_hash"] for s in manifest["shards"]]
        mode = orch.classify_package(
            "2.4.0", manifest["generation_floor"],
            existing + ["h-extra"],
        )
        self.assertEqual(mode, "incremental_append")
        orch.close()


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.harness.load()

    def tearDown(self):
        self.harness.cleanup()

    def test_parallel_failure_domain_processes_never_double_claim(self):
        db = self.harness.db_path
        samples = self.harness.sample_dir
        env = dict(os.environ, PYTHONPATH=ROOT)
        commands = [
            [sys.executable, "-m", "scaler", "--db", db,
             "--samples", samples, "reconcile", "--failure-domain", fd]
            for fd in ("fd-a", "fd-b", "fd-c")
        ]
        processes = [
            subprocess.Popen(
                cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            for cmd in commands
        ]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(
                process.returncode, 0,
                stderr.decode() or stdout.decode(),
            )
            outputs.append(stdout.decode())
        # each process opens its own round; inspect the union of action rows
        connection = sqlite3.connect(db)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT round_no, service, COUNT(*) AS n FROM actions "
            "GROUP BY round_no, service HAVING n > 1"
        ).fetchall()
        self.assertEqual(rows, [])
        negatives = connection.execute(
            "SELECT * FROM node_states WHERE cpu_free < 0 OR mem_free < 0"
        ).fetchall()
        self.assertEqual(negatives, [])
        connection.close()


if __name__ == "__main__":
    unittest.main()
