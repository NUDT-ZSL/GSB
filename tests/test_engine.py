import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sampling_bench.dataset import generate_dataset
from sampling_bench.engine import BenchEngine, InterruptedRun, RunConfig
from sampling_bench.errors import E_BATCH_MISMATCH, E_INCONSISTENT
from sampling_bench.models import SampleResult
from sampling_bench.scheduler import BatchTask, Scheduler, first_divergence
from sampling_bench.selfcheck import run_selfcheck


class EngineIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data_dir = Path(self.root) / "data"
        generate_dataset(self.data_dir, num_requests=10, seed=7777)
        self.config = RunConfig(max_workers=2, batch_size=2)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_full_run_and_selfcheck(self):
        engine = BenchEngine(self.data_dir, Path(self.root) / "work", self.config)
        artifacts = engine.run(fresh=True)
        self.assertEqual(len(artifacts.results), 10)
        effective = {
            decision.request_id: decision.target_length
            for decision in artifacts.plan.selected.decisions
            if decision.admitted
        }
        report = run_selfcheck(
            valid_requests=engine.validate().valid_requests,
            admitted_ids=artifacts.plan.selected.admitted_ids,
            rejected_ids=[
                d.request_id for d in artifacts.plan.selected.rejected
            ],
            results=artifacts.results,
            ledger=artifacts.ledger,
            effective_lengths=effective,
        )
        self.assertTrue(report.ok)

    def test_first_divergence_locates_step(self):
        engine = BenchEngine(self.data_dir, Path(self.root) / "work_div", self.config)
        artifacts = engine.run(fresh=True)
        good = artifacts.results[0]
        bad = SampleResult.from_dict(good.to_dict())
        bad.generated_tokens[1] = (bad.generated_tokens[1] + 1) % 32
        self.assertEqual(first_divergence(good, bad), 1)
        self.assertIsNone(first_divergence(good, SampleResult.from_dict(good.to_dict())))

    def test_scheduler_verify_raises_batch_mismatch(self):
        from sampling_bench.dataset import load_requests

        requests = load_requests(self.data_dir)[:2]
        engine = BenchEngine(self.data_dir, Path(self.root) / "work_div2", self.config)
        tasks = [
            BatchTask(request=req, effective_length=req.target_length, override_params=None)
            for req in requests
        ]
        good_results = engine.run(fresh=True).results[:2]
        tampered = SampleResult.from_dict(good_results[0].to_dict())
        tampered.generated_tokens[0] = (tampered.generated_tokens[0] + 1) % 32
        pooled = [tampered, good_results[1]]
        pooled.sort(key=lambda result: result.request_id)

        scheduler_obj = Scheduler(
            self.data_dir, engine.vocab, max_workers=1, batch_size=2, verify=True
        )
        ordered_tasks = sorted(tasks, key=lambda task: task.request.request_id)
        with self.assertRaises(Exception) as ctx:
            scheduler_obj._verify_batch(ordered_tasks, pooled)
        self.assertEqual(ctx.exception.code, E_BATCH_MISMATCH)
        self.assertIsNotNone(ctx.exception.request_id)
        self.assertEqual(ctx.exception.detail["first_divergence_step"], 0)

    def test_crash_recovery_byte_identical(self):
        crash_engine = BenchEngine(
            self.data_dir, Path(self.root) / "work_crash", self.config
        )
        with self.assertRaises(InterruptedRun):
            crash_engine.run(fresh=True, crash_after_committed=4)

        resumed_engine = BenchEngine(
            self.data_dir, Path(self.root) / "work_crash", self.config
        )
        resumed = resumed_engine.run(fresh=False)

        baseline_engine = BenchEngine(
            self.data_dir, Path(self.root) / "work_baseline", self.config
        )
        baseline = baseline_engine.run(fresh=True)

        self.assertEqual(
            [r.to_dict() for r in resumed.results],
            [r.to_dict() for r in baseline.results],
        )
        self.assertTrue(resumed.recovery.resumed)
        self.assertTrue(resumed.recovery.disjoint)
        self.assertEqual(
            set(resumed.recovery.committed_ids)
            & set(resumed.recovery.reexecuted_ids),
            set(),
        )
        self.assertEqual(len(resumed.recovery.committed_ids), 4)

    def test_inconsistent_state_detected(self):
        engine = BenchEngine(self.data_dir, Path(self.root) / "work_inc", self.config)
        artifacts = engine.run(fresh=True)
        effective = {
            decision.request_id: decision.target_length
            for decision in artifacts.plan.selected.decisions
            if decision.admitted
        }
        # 人为制造不一致：把一个已采样请求同时标记为被拒（被拒请求不得出现在结果中）。
        leaked = artifacts.plan.selected.admitted_ids[0]
        with self.assertRaises(Exception) as ctx:
            run_selfcheck(
                valid_requests=engine.validate().valid_requests,
                admitted_ids=artifacts.plan.selected.admitted_ids,
                rejected_ids=[leaked],
                results=artifacts.results,
                ledger=artifacts.ledger,
                effective_lengths=effective,
            )
        self.assertEqual(ctx.exception.code, E_INCONSISTENT)

    def test_snapshot_versions_increment_across_runs(self):
        work = Path(self.root) / "work_versions"
        first = BenchEngine(self.data_dir, work, self.config).run(fresh=True)
        second = BenchEngine(self.data_dir, work, self.config).run(fresh=True)
        for name in ("samples", "ledger", "decisions"):
            self.assertEqual(second.snapshot_versions[name], first.snapshot_versions[name] + 1)


if __name__ == "__main__":
    unittest.main()
