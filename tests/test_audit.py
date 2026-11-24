from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from sbom_audit.checkpoint import CheckpointManager
from sbom_audit.cli import main as cli_main
from sbom_audit.engine import AuditEngine, Batch, Budget, BatchScheduler
from sbom_audit.graph import DependencyGraph
from sbom_audit.model import AuditError
from sbom_audit.parser import parse_dependency_file, parse_v1_file
from sbom_audit.samplegen import generate_sample
from sbom_audit.verify import verify_sample


class SampleAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="sbom-test-")
        cls.root = Path(cls.temp.name)
        cls.sample = cls.root / "sample"
        cls.truth = generate_sample(cls.sample)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_deterministic_acceptance_verifier(self) -> None:
        result = verify_sample(self.sample, self.root / "verify-work")
        self.assertTrue(result["verified"])
        self.assertGreater(result["checks_passed"], 30)

    def test_invalid_records_continue_and_have_codes(self) -> None:
        parsed = parse_v1_file(self.sample / "invalid" / "v1_invalid_records.json")
        codes = {item.code for item in parsed.rejections}
        self.assertTrue({
            "E_INVALID_ID",
            "E_VERSION_SYNTAX",
            "E_HASH_ALGO",
            "E_SIGNATURE_STATUS",
            "E_TIMESTAMP",
            "E_DUPLICATE_KEY",
        } <= codes)
        self.assertEqual(len(parsed.records), 2)
        for rejection in parsed.rejections:
            self.assertTrue(rejection.snapshot.startswith("v1:"))
            self.assertGreaterEqual(rejection.index, 1)

    def test_cycle_detection_lists_keys(self) -> None:
        edges, rejections, _ = parse_dependency_file(self.sample / "invalid" / "dependency_cycles.json")
        self.assertFalse(rejections)
        graph = DependencyGraph(edges)
        cycles = graph.find_cycles()
        keys = {key for cycle in cycles for key in cycle[:-1]}
        self.assertIn("pkg:generic/cycle-a", keys)
        self.assertIn("pkg:generic/self-loop", keys)
        with self.assertRaises(AuditError) as ctx:
            graph.require_acyclic()
        self.assertEqual(ctx.exception.code, "E_DEP_CYCLE")

    def test_timeout_queue_and_work_budget(self) -> None:
        ledger = self.root / "budget.sqlite3"
        engine = AuditEngine(ledger)
        timeout = engine.apply_batch(
            Batch("timeout", work_items=[{"canonical_key": "x", "bytes": 4 * 1024 * 1024}]),
            Budget(batch_timeout_ms=0, max_digest_bytes_per_component=10**12),
        )
        self.assertEqual(timeout["code"], "E_TIMEOUT")
        self.assertFalse(timeout["transaction_committed"])

        budget = engine.apply_batch(
            Batch("work-limit", work_items=[{"canonical_key": "x", "bytes": 11}]),
            Budget(max_digest_bytes_per_component=10),
        )
        self.assertEqual(budget["code"], "E_HASH_WORK_LIMIT")

        scheduler = BatchScheduler(self.root / "queue.sqlite3", Budget(concurrency=1, queue_limit=0))
        started = threading.Event()
        proceed = threading.Event()
        accepted, future = scheduler.submit(Batch("one", started_event=started, proceed_event=proceed))
        self.assertTrue(accepted)
        self.assertTrue(started.wait(2))
        queued, rejected = scheduler.submit(Batch("two"))
        self.assertFalse(queued)
        self.assertEqual(rejected["code"], "E_QUEUE_FULL")
        proceed.set()
        self.assertEqual(future.result()["status"], "applied")

    def test_checkpoint_policy_change_forces_rebuild(self) -> None:
        work = self.root / "checkpoint-case"
        engine = AuditEngine(work / "ledger.sqlite3")
        v1 = parse_v1_file(self.sample / "v1_snapshot.json")
        result = engine.apply_batch(Batch("v1", records=v1.records))
        self.assertNotEqual(result.get("code"), "E_RECOMPUTE_DIVERGED")
        manager = CheckpointManager(engine.ledger.path, work / "checkpoint.json")
        manager.save(engine.report)
        self.assertTrue(manager.verify()["valid"])

        checkpoint_path = work / "checkpoint.json"
        data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        data["merge_policy_version"] = "old"
        checkpoint_path.write_text(json.dumps(data), encoding="utf-8")
        verified = manager.verify()
        self.assertEqual(verified["code"], "E_CHECKPOINT_VERSION")
        self.assertTrue(verified["rebuild_required"])

    def test_transaction_failure_rolls_back_entire_batch(self) -> None:
        ledger = self.root / "rollback.sqlite3"
        engine = AuditEngine(ledger)
        record = {
            "canonical_key": "pkg:generic/rollback-probe",
            "component_id": "rollback-probe",
            "version": "1.0.0",
            "algorithm": "sha256",
            "digest": "a" * 64,
            "signature_status": "signed",
            "published_at_ms": 1795000000000,
            "generation": 1,
            "snapshot_kind": "test",
            "snapshot_id": "rollback",
            "source_ref": "test",
            "record_index": 1,
        }
        result = engine.apply_batch(Batch("rollback-batch", records=[record], edges=[{"bad": "edge"}]))
        self.assertEqual(result["code"], "E_LEDGER")
        reopened = AuditEngine(ledger)
        self.assertEqual(reopened.records, [])
        conn = reopened.ledger.connect()
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_cli_usage_error_code_two(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            cli_main(["unknown-command"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
