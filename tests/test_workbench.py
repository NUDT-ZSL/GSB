from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from gemm_bmm_workbench.canonical import read_json
from gemm_bmm_workbench.errors import Codes, WorkbenchError
from gemm_bmm_workbench.kernels import reference_multiply, tiled_multiply_accumulate, verify_against_reference
from gemm_bmm_workbench.ledger import LedgerManager, guard_schema_transition
from gemm_bmm_workbench.resources import Budget, CommitLock
from gemm_bmm_workbench.runtime import Workbench
from gemm_bmm_workbench.sampling import deterministic_arrays, scenario_batches
from gemm_bmm_workbench.selfcheck import run_selfcheck
from gemm_bmm_workbench.specs import normalize_job


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def base_job(**overrides):
    job = {
        "job_id": "case",
        "op_kind": "gemm",
        "matrix_shapes": {"A": [6, 5], "B": [5, 7], "C": [6, 7]},
        "dtype": "float64",
        "layout": "row_major",
        "seed": 42,
        "block_size": 3,
        "alpha": 1.0,
        "beta": 0.5,
        "revision": 1,
    }
    job.update(overrides)
    return job


class TemporaryStateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="wb-test-"))
        self.state = self.temp / "state"
        self.bench = Workbench(self.state)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)


class ValidationTests(TemporaryStateTestCase):
    def assert_code(self, code: str, fn, *args, **kwargs):
        with self.assertRaises(WorkbenchError) as context:
            fn(*args, **kwargs)
        self.assertEqual(context.exception.code, code)
        return context.exception

    def test_basic_gemm_executes(self):
        result = self.bench.submit_records([base_job()])
        self.assertTrue(result["ok"])
        self.assertEqual(result["job_count"], 1)
        self.assertTrue((self.state / "results" / "case.json").exists())

    def test_structured_validation_codes(self):
        self.assert_code(
            Codes.SHAPE_INVALID,
            normalize_job,
            base_job(matrix_shapes={"A": [2, 3], "B": [4, 5]}),
        )
        self.assert_code(
            Codes.SHAPE_INVALID,
            normalize_job,
            base_job(
                op_kind="bmm",
                matrix_shapes={"A": [2, 4, 3], "B": [3, 3, 5]},
            ),
        )
        self.assert_code(Codes.DTYPE_UNSUPPORTED, normalize_job, base_job(dtype="int8"))
        self.assert_code(Codes.LAYOUT_INVALID, normalize_job, base_job(layout="tiled"))
        self.assert_code(Codes.BLOCK_INVALID, normalize_job, base_job(block_size=0))

    def test_array_dimension_mismatch_code(self):
        spec = normalize_job(base_job())
        arrays = deterministic_arrays(spec)
        arrays["A"] = np.zeros((9, 9), dtype=np.float64)
        with self.assertRaises(WorkbenchError) as ctx:
            self.bench.submit_records([base_job()], arrays_by_job={"case": arrays})
        self.assertEqual(ctx.exception.code, Codes.SHAPE_INVALID)
        self.assertEqual(ctx.exception.details["reason"], "matrix_data_dimension_mismatch")

    def test_duplicate_and_revision_regression(self):
        self.assert_code(
            Codes.JOB_DUPLICATE,
            self.bench.submit_records,
            [base_job(), base_job(seed=43)],
        )
        self.bench.submit_records([base_job(revision=2)])
        self.assert_code(Codes.REVISION_REGRESSION, self.bench.submit_records, [base_job(revision=1)])


class KernelTests(unittest.TestCase):
    def test_tiled_matches_float64_reference(self):
        spec = normalize_job(base_job(op_kind="bmm", matrix_shapes={"A": [2, 7, 5], "B": [2, 5, 8], "C": [2, 7, 8], "block_size": 3}))
        arrays = deterministic_arrays(spec)
        result = tiled_multiply_accumulate(spec, arrays)
        spectrum = verify_against_reference(spec, result["output"], arrays)
        reference = reference_multiply(spec, arrays)
        self.assertEqual(result["output"].shape, reference.shape)
        self.assertLess(spectrum["max_abs_err"], 1e-9)
        self.assertIn("batch", result["loop_order"])

    def test_tolerance_failure_reports_first_position(self):
        spec = normalize_job(base_job())
        arrays = deterministic_arrays(spec)
        output = tiled_multiply_accumulate(spec, arrays)["output"]
        faulty = output.copy()
        faulty[0, 2, 1] += 5.0
        with self.assertRaises(WorkbenchError) as ctx:
            verify_against_reference(spec, faulty, arrays)
        self.assertEqual(ctx.exception.code, Codes.TOLERANCE_EXCEEDED)
        self.assertEqual(ctx.exception.details["first_deviation"]["index"], [0, 2, 1])


class CacheIncrementalTests(TemporaryStateTestCase):
    def test_parameter_revision_reuses_plan_and_byte_equivalence(self):
        batch_one, batch_two = scenario_batches()
        self.bench.submit_records(batch_one)
        incremental = self.bench.submit_records(batch_two)
        impact = read_json(self.state / "incremental-report.json")
        self.assertIn("bmm_alpha:validation_plan", impact["reused_nodes"])
        self.assertNotIn("bmm_alpha:block_accumulate", impact["reused_nodes"])
        self.assertTrue(impact["coverage"]["intersection_empty"])
        self.assertTrue(impact["coverage"]["union_covers_all_graph_nodes"])

        full_state = self.temp / "full"
        full_bench = Workbench(full_state)
        full_bench.submit_records(batch_two)
        for path in sorted((self.state / "results").glob("*.json")):
            self.assertEqual(path.read_bytes(), (full_state / "results" / path.name).read_bytes())
        report = run_selfcheck(self.state)
        self.assertTrue(report["ok"], json.dumps(report, indent=2)[:4000])
        self.assertTrue(incremental["ok"])

    def test_cache_query_contains_required_key_factors(self):
        self.bench.submit_records([base_job(alpha=0.25, beta=0.75)])
        query = self.bench.query_cache("case")
        composition = query["nodes"]["block_accumulate"]["key_report"]["key_composition"]
        for factor in (
            "array_hashes",
            "op_kind",
            "matrix_shapes",
            "dtype",
            "layout",
            "block_size",
            "alpha",
            "beta",
            "upstream_product_hash",
        ):
            self.assertIn(factor, composition)


class LedgerResourceLockTests(TemporaryStateTestCase):
    def test_ledger_migration_semantics_and_version_guard(self):
        ledger = LedgerManager(self.state)
        ledger.write_v1_rows(
            [
                {"relative_commit_seconds": 0, "job_id": "a", "output_digest": "a" * 64},
                {"relative_commit_seconds": 1, "job_id": "b", "output_digest": "b" * 64},
            ]
        )
        report = ledger.migrate_v1_to_v2({"a": {"input_hash": "i" * 64}})
        self.assertEqual(report["migrated_count"], 2)
        self.assertTrue(all("algebra_generation" in item["sources"] for item in report["item_sources"]))
        self.assertTrue(all(pair["semantic_equal"] for pair in ledger.semantic_pairs()))
        down = ledger.migrate_v2_to_v1()
        self.assertEqual(down["migrated_count"], 2)
        with self.assertRaises(WorkbenchError) as ctx:
            guard_schema_transition(2, 1)
        self.assertEqual(ctx.exception.code, Codes.LEDGER_VERSION_REGRESSION)
        guard_schema_transition(2, 1, allow_downgrade=True)

    def test_job_limit_and_block_degradation(self):
        bench = Workbench(self.temp / "limited", budget=Budget(max_jobs_per_stage=1))
        with self.assertRaises(WorkbenchError) as ctx:
            bench.submit_records([base_job(job_id="a"), base_job(job_id="b")])
        self.assertEqual(ctx.exception.code, Codes.RESOURCE_LIMIT)

        small = Workbench(
            self.temp / "small-budget",
            budget=Budget(max_accum_buffer_bytes=600, resource_policy="degrade"),
        )
        result = small.submit_records([base_job(matrix_shapes={"A": [8, 6], "B": [6, 7], "C": [8, 7]}, block_size=4)])
        state = small.load_committed_state()
        effective = state["jobs"]["case"]["effective_block_size"]
        self.assertLess(effective, 4)
        self.assertTrue(state["jobs"]["case"]["degradations"])
        self.assertEqual(result["ok"], True)

    def test_nonblocking_commit_lock_busy(self):
        lock = CommitLock(self.state / "locks" / "commit.lock").acquire()
        try:
            with self.assertRaises(WorkbenchError) as ctx:
                CommitLock(self.state / "locks" / "commit.lock").acquire()
            self.assertEqual(ctx.exception.code, Codes.STAGE_BUSY)
        finally:
            lock.release()


class RecoverySubprocessTests(unittest.TestCase):
    def test_sigkill_recovery_matches_control(self):
        temp = Path(tempfile.mkdtemp(prefix="wb-recovery-"))
        try:
            state = temp / "recovery"
            env = dict(**__import__("os").environ, PYTHONPATH=str(PROJECT_ROOT))
            jobs = PROJECT_ROOT / "samples" / "batches" / "batch-001.jsonl"
            if not jobs.exists():
                subprocess.run(
                    [sys.executable, "-m", "gemm_bmm_workbench.cli", "generate-samples", "--out", "samples"],
                    cwd=PROJECT_ROOT,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            killed = subprocess.run(
                [sys.executable, "-m", "gemm_bmm_workbench.cli", "run", "--jobs", str(jobs), "--state-dir", str(state), "--kill-after-chunks", "2"],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(killed.returncode, 0, killed.stdout + killed.stderr)
            resume = subprocess.run(
                [sys.executable, "-m", "gemm_bmm_workbench.cli", "resume", "--state-dir", str(state)],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(resume.returncode, 0, resume.stdout + resume.stderr)
            report = read_json(state / "selfcheck.json") if (state / "selfcheck.json").exists() else run_selfcheck(state)
            self.assertTrue(report["ok"], json.dumps(report, indent=2)[:4000])
        finally:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
