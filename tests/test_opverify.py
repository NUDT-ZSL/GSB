from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from opverify.consistency import run_self_check, self_check_response
from opverify.engine import evaluate_job, run_unified
from opverify.errors import OpVerifyError
from opverify.ledger import CommitLock
from opverify.samples import generate_sample_set, read_jsonl
from opverify.service import VerificationService
from opverify.snapshots import SnapshotStore, make_envelope
from opverify.verify import compare_outputs


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class OpVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.temp = Path(self._temp.name)
        self.sample = self.temp / "sample"
        generate_sample_set(self.sample)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_sample_validation_error_codes(self) -> None:
        cases = read_jsonl(self.sample / "invalid_cases.jsonl")
        for case in cases:
            response = run_unified(
                case["record"],
                base_dir=self.sample,
                prior_revision=case.get("prior_revision"),
            )
            self.assertFalse(response["ok"], case)
            self.assertEqual(response["error"]["code"], case["expect_code"], case)
            self.assertTrue(response["error"]["job_id"])
            self.assertTrue(response["error"]["field"].startswith("$"))

    def test_all_operators_match_reference_and_are_reproducible(self) -> None:
        records = read_jsonl(self.sample / "tasks_v1.jsonl")
        self.assertEqual(len(records), 11)
        for record in records:
            with self.subTest(job_id=record["job_id"]):
                first = evaluate_job(record, base_dir=self.sample)
                second = evaluate_job(record, base_dir=self.sample)
                self.assertTrue(first["comparison"]["ok"])
                self.assertEqual(
                    first["reference"].tobytes(), second["reference"].tobytes()
                )
                self.assertEqual(first["output"].tobytes(), second["output"].tobytes())
                self.assertLess(first["comparison"]["max_absolute_error"], 1e-8)

    def test_full_incremental_sets_self_check_and_rollback(self) -> None:
        root = self.temp / "ledger"
        service = VerificationService(root)
        full = service.run_manifest(self.sample / "tasks_v1.jsonl", batch_id="full")
        self.assertTrue(full["ok"], full)
        self.assertEqual(full["ledger_version"], 1)
        self.assertTrue(run_self_check(root, self.sample)["ok"])

        block = service.run_manifest(
            self.sample / "update_block_policy.jsonl", batch_id="block"
        )
        self.assertEqual(block["ledger_version"], 2)
        self.assertEqual(
            [(x["job_id"], x["stages"]) for x in block["recompute"]],
            [("inc_block", ["plan", "chunk"])],
        )
        self.assertIn("inc_block_down", block["fully_reused_jobs"])

        geometry = service.run_manifest(
            self.sample / "update_geometry.jsonl", batch_id="geometry"
        )
        self.assertEqual(geometry["ledger_version"], 3)
        recompute = {x["job_id"]: x["stages"] for x in geometry["recompute"]}
        self.assertEqual(
            recompute["inc_geom"], ["reference", "plan", "chunk"]
        )
        self.assertEqual(recompute["inc_geom_down"], ["chunk"])

        shape = service.run_manifest(self.sample / "update_shape.jsonl", batch_id="shape")
        self.assertEqual(shape["ledger_version"], 4)
        recompute = {x["job_id"]: x["stages"] for x in shape["recompute"]}
        self.assertEqual(
            recompute["inc_shape"], ["input", "reference", "plan", "chunk"]
        )
        self.assertEqual(recompute["inc_shape_down"], ["chunk"])
        self.assertTrue(run_self_check(root, self.sample)["ok"])

        old_rollback = dict(read_jsonl(self.sample / "update_block_policy.jsonl")[0])
        old_rollback["revision"] = 1
        with self.assertRaises(OpVerifyError) as ctx:
            service.run_records([old_rollback], batch_id="rollback")
        self.assertEqual(ctx.exception.code, "E_REVISION_ROLLBACK")

    def test_snapshot_rollback_is_rejected(self) -> None:
        store = SnapshotStore(self.temp / "snapshots")
        store.commit_bundle([make_envelope("ledger", 2, {"value": 2})])
        with self.assertRaises(OpVerifyError) as ctx:
            store.replace_current(make_envelope("ledger", 1, {"value": 1}))
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_ROLLBACK")

    def test_lock_returns_stage_busy_immediately(self) -> None:
        lock_path = self.temp / "commit.lock"
        ready = threading.Event()
        release = threading.Event()

        def holder() -> None:
            lock = CommitLock.acquire(lock_path)
            ready.set()
            release.wait(2)
            lock.release()

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(ready.wait(2))
        try:
            with self.assertRaises(OpVerifyError) as ctx:
                CommitLock.acquire(lock_path)
            self.assertEqual(ctx.exception.code, "E_STAGE_BUSY")
        finally:
            release.set()
            thread.join(2)

    def test_tolerance_error_reports_first_position(self) -> None:
        record = next(r for r in read_jsonl(self.sample / "tasks_v1.jsonl") if r["job_id"] == "conv_basic")
        result = evaluate_job(record, base_dir=self.sample)
        bad = result["output"].copy()
        bad[0, 0, 1, 2] += 0.25
        with self.assertRaises(OpVerifyError) as ctx:
            compare_outputs(result["reference"], bad)
        self.assertEqual(ctx.exception.code, "E_TOLERANCE_EXCEEDED")
        self.assertEqual(
            ctx.exception.details["first_mismatch"]["position"], [0, 0, 1, 2]
        )

    def test_tampered_snapshot_fails_self_check(self) -> None:
        root = self.temp / "tampered"
        service = VerificationService(root)
        service.run_manifest(self.sample / "tasks_v1.jsonl", batch_id="full")
        outputs_path = root / "snapshots" / "outputs-current.json"
        envelope = json.loads(outputs_path.read_text(encoding="utf-8"))
        envelope["entries"][0]["output_digest"] = "0" * 64
        outputs_path.write_text(json.dumps(envelope), encoding="utf-8")
        response = self_check_response(root, base_dir=self.sample)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "E_INCONSISTENT")
        self.assertIn("first_difference", response["error"]["details"])

    def test_sigkill_recovery_matches_one_shot(self) -> None:
        manifest = self.sample / "tasks_v1.jsonl"
        one_shot_root = self.temp / "one-shot"
        crash_root = self.temp / "crash"
        one_shot = VerificationService(one_shot_root)
        expected = one_shot.run_manifest(manifest, batch_id="full")["snapshot_checksums"]

        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
        crash = subprocess.run(
            [
                sys.executable,
                "-m",
                "opverify.cli",
                "run",
                "--root",
                str(crash_root),
                "--manifest",
                str(manifest),
                "--batch-id",
                "full",
                "--crash-after-commits",
                "2",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(crash.returncode, -9, crash.stderr)

        resume = subprocess.run(
            [
                sys.executable,
                "-m",
                "opverify.cli",
                "run",
                "--root",
                str(crash_root),
                "--manifest",
                str(manifest),
                "--batch-id",
                "full",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(resume.returncode, 0, resume.stderr)
        report = json.loads(resume.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["snapshot_checksums"], expected)
        skipped_chunks = [
            item["job_id"]
            for item in report["execution"]["skipped_committed"]
            if item["stage"] == "chunk"
        ]
        self.assertEqual(len(skipped_chunks), 2)
        self.assertTrue(run_self_check(crash_root, self.sample)["ok"])


if __name__ == "__main__":
    unittest.main()
