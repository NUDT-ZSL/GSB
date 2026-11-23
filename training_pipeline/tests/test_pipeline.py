"""Unit / integration tests for the deterministic training data pipeline.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datapipe.assembler import serial_truth_batches, verify_coverage, compare_with_truth, worker_shard_assignment
from datapipe.errors import (
    E_BATCH_DIGEST_CONFLICT,
    E_CACHE_ALGEBRA_ROLLBACK,
    E_CLOCK_ROLLBACK,
    E_DUPLICATE_NAME,
    E_DIVISIBILITY,
    E_GEN_DIGEST_MISMATCH,
    E_PREFETCH_FAILED,
    E_PREFETCH_TIMEOUT,
    E_VALIDATION,
    PipelineError,
)
from datapipe.loader import load_scenario
from datapipe.pipeline import Pipeline, plan_rounds
from datapipe.sharding import build_shard_manifest, fixed_sample_stream

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")


class DeterminismTests(unittest.TestCase):
    def test_manifest_bit_stable(self):
        scenario = load_scenario(os.path.join(SAMPLES, "baseline"))
        for spec in scenario.datasets:
            first = build_shard_manifest(spec, 0)
            second = build_shard_manifest(spec, 0)
            self.assertEqual(first.digest, second.digest)
            self.assertEqual(first.byte_size, second.byte_size)
            self.assertEqual(
                [sample.identity for sample in first.samples],
                [sample.identity for sample in second.samples],
            )

    def test_sample_content_independent_of_order(self):
        scenario = load_scenario(os.path.join(SAMPLES, "baseline"))
        spec = scenario.datasets[0]
        forward = [sample.identity for sample in fixed_sample_stream(spec)]
        reverse_manifests = [build_shard_manifest(spec, i) for i in range(spec.shard_count)][::-1]
        rebuilt = [sample.identity for manifest in reverse_manifests for sample in manifest.samples]
        self.assertEqual(sorted(forward), sorted(rebuilt))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(SAMPLES, "baseline")
        self.dst = os.path.join(self.tmp, "scenario")
        shutil.copytree(self.src, self.dst)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _edit(self, name, mutate):
        path = os.path.join(self.dst, name)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        mutate(data)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_divisibility_rejected_without_persisting(self):
        def mutate(data):
            data[0]["total_samples"] = 13

        self._edit("datasets.json", mutate)
        with self.assertRaises(PipelineError) as ctx:
            load_scenario(self.dst)
        self.assertEqual(ctx.exception.code, E_DIVISIBILITY)
        self.assertEqual(os.listdir(self.dst).count("pipeline.sqlite3"), 0)

    def test_duplicate_dataset_name(self):
        self._edit("datasets.json", lambda data: data.append(dict(data[0])))
        with self.assertRaises(PipelineError) as ctx:
            load_scenario(self.dst)
        self.assertEqual(ctx.exception.code, E_DUPLICATE_NAME)

    def test_bad_injection_reference(self):
        def mutate(data):
            data.append({"stage": "generate", "kind": "gen_digest_mismatch", "target": "nope#s000000"})

        self._edit("injections.json", mutate)
        with self.assertRaises(PipelineError):
            load_scenario(self.dst)


class InvalidationTests(unittest.TestCase):
    def test_precise_invalidation(self):
        scenario = load_scenario(os.path.join(SAMPLES, "invalidation"))
        work = tempfile.mkdtemp()
        try:
            pipeline = Pipeline(work, scenario)
            report = pipeline.warm_cache_from_view()
            self.assertIn("images#s000000", report["invalidated"])
            self.assertIn("text#s000000", report["retained"])
            self.assertEqual(report["trigger"], "gen_params_or_content_version")
            pipeline.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)


class SchedulingTests(unittest.TestCase):
    def _run_fault(self, scenario_name):
        scenario = load_scenario(os.path.join(SAMPLES, scenario_name))
        work = tempfile.mkdtemp()
        try:
            pipeline = Pipeline(work, scenario)
            result = pipeline.run(assemble=False)
            codes = {r.error_code for rd in result.round_reports for r in rd.results if r.error_code}
            pipeline.close()
            return codes
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_gen_digest_mismatch_code(self):
        self.assertEqual(self._run_fault("fault_gen_digest"), {E_GEN_DIGEST_MISMATCH})

    def test_cache_algebra_rollback_code(self):
        self.assertEqual(self._run_fault("fault_cache_rollback"), {E_CACHE_ALGEBRA_ROLLBACK})

    def test_budget_three_outcomes(self):
        scenario = load_scenario(os.path.join(SAMPLES, "budget_exhausted"))
        work = tempfile.mkdtemp()
        try:
            pipeline = Pipeline(work, scenario)
            result = pipeline.run(assemble=False)
            outcomes = {r.outcome for rd in result.round_reports for r in rd.results}
            self.assertIn("skipped_budget", outcomes)
            self.assertTrue(any(not r.started for rd in result.round_reports for r in rd.results
                                if r.outcome == "skipped_budget"))
            pipeline.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_clock_driven_timeout_distinct_code(self):
        scenario = load_scenario(os.path.join(SAMPLES, "fault_timeout"))
        work = tempfile.mkdtemp()
        try:
            pipeline = Pipeline(work, scenario)
            result = pipeline.run(assemble=False)
            codes = {r.error_code for rd in result.round_reports for r in rd.results if r.error_code}
            self.assertEqual(codes, {E_PREFETCH_TIMEOUT})
            pipeline.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_clock_rollback_code(self):
        scenario = load_scenario(os.path.join(SAMPLES, "fault_clock_rollback"))
        work = tempfile.mkdtemp()
        try:
            pipeline = Pipeline(work, scenario)
            result = pipeline.run(assemble=False)
            codes = {r.error_code for rd in result.round_reports for r in rd.results if r.error_code}
            self.assertIn(E_CLOCK_ROLLBACK, codes)
            pipeline.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_prefetch_failure_code(self):
        scenario = load_scenario(os.path.join(SAMPLES, "fault_prefetch"))
        work = tempfile.mkdtemp()
        try:
            pipeline = Pipeline(work, scenario)
            result = pipeline.run(assemble=False)
            codes = {r.error_code for rd in result.round_reports for r in rd.results if r.error_code}
            self.assertEqual(codes, {E_PREFETCH_FAILED})
            pipeline.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)


class AssemblyTests(unittest.TestCase):
    def test_worker_assignment_disjoint(self):
        scenario = load_scenario(os.path.join(SAMPLES, "baseline"))
        specs = {spec.name: spec for spec in scenario.datasets}
        assignment = worker_shard_assignment(scenario.job, specs)
        all_shards = [shard for shards in assignment.values() for shard in shards]
        self.assertEqual(len(all_shards), len(set(all_shards)))

    def test_full_run_matches_serial_truth(self):
        scenario = load_scenario(os.path.join(SAMPLES, "baseline"))
        work = tempfile.mkdtemp()
        try:
            pipeline = Pipeline(work, scenario)
            result = pipeline.run()
            truth = serial_truth_batches(scenario)
            compare_with_truth(scenario, result.batches)
            verify_coverage(scenario, result.batches)
            self.assertEqual([b.digest for b in truth], [b.digest for b in result.batches])
            pipeline.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_batch_conflict_code(self):
        scenario = load_scenario(os.path.join(SAMPLES, "fault_batch_conflict"))
        work = tempfile.mkdtemp()
        try:
            pipeline = Pipeline(work, scenario)
            with self.assertRaises(PipelineError) as ctx:
                pipeline.run()
            self.assertEqual(ctx.exception.code, E_BATCH_DIGEST_CONFLICT)
            pipeline.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)


class IdempotencyTests(unittest.TestCase):
    def test_repeated_shard_and_batch_commit_idempotent(self):
        scenario = load_scenario(os.path.join(SAMPLES, "baseline"))
        work = tempfile.mkdtemp()
        try:
            pipeline = Pipeline(work, scenario)
            pipeline.run()
            shard_count = len(pipeline.store.committed_shard_rows())
            batch_count = len(pipeline.store.batch_rows())
            # Re-running assembly must not duplicate any batch record.
            from datapipe.assembler import Assembler
            from datapipe.injection import InjectionEngine

            assembler = Assembler(scenario, pipeline.store, InjectionEngine(()))
            assembler.assemble()
            self.assertEqual(len(pipeline.store.committed_shard_rows()), shard_count)
            self.assertEqual(len(pipeline.store.batch_rows()), batch_count)
            pipeline.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)


class RecoveryTests(unittest.TestCase):
    def test_crash_resume_replays_only_missing(self):
        scenario = load_scenario(os.path.join(SAMPLES, "fault_crash_generate"))
        work = tempfile.mkdtemp()
        try:
            from datapipe.injection import ForcedCrash

            pipeline = Pipeline(work, scenario)
            with self.assertRaises(ForcedCrash):
                pipeline.run(assemble=False)
            pipeline.close()

            resumed = Pipeline(work, scenario)
            result = resumed.run()
            self.assertTrue(result.recovered)
            self.assertIn("images#s000000", result.recovery_detail["replayed_missing"])
            # No double counting in memory or cache.
            self.assertEqual(
                resumed.store.conn.execute(
                    "SELECT COUNT(*) FROM (SELECT 1 FROM memory_ledger GROUP BY dataset,shard_index,algebra "
                    "HAVING COUNT(*)>1)"
                ).fetchone()[0],
                0,
            )
            compare_with_truth(scenario, result.batches)
            resumed.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
