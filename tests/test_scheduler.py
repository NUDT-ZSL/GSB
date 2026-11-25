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

import generator  # noqa: E402
from gpu_scheduler.cli import main as cli_main  # noqa: E402
from gpu_scheduler.engine import SchedulerEngine  # noqa: E402
from gpu_scheduler.errors import SchedulerError  # noqa: E402
from gpu_scheduler.models import load_bundle, validate_bundle  # noqa: E402
from gpu_scheduler.storage import SchedulerStore  # noqa: E402
from gpu_scheduler.verify import verify_injection_isolation  # noqa: E402


SAMPLES = ROOT / "data" / "samples"


def setUpModule() -> None:
    generator.main()


class SchedulerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="gpu-scheduler-test-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "results" / "test.db"
        self.bundle = load_bundle(SAMPLES)
        self.engine = SchedulerEngine(SchedulerStore(self.db), self.bundle)
        self.engine.initialize_bundle()

    def grant(self, job_id: str, *, now: int = 100, card: str | None = None, round_id: str | None = None):
        request = {
            "action": "grant",
            "stage": "grant",
            "round_id": round_id or f"r-{job_id}",
            "now_ms": now,
            "job_id": job_id,
        }
        if card:
            request["card_id"] = card
        return self.engine.submit(request)

    def setup_fragmented_card(self) -> None:
        for index in range(1, 6):
            self.grant(f"job-frag-{index:02d}", card="gpu-frag", round_id=f"frag-grant-{index}")
        self.engine.submit(
            {
                "action": "renew",
                "stage": "renew",
                "round_id": "frag-renew",
                "now_ms": 150,
                "job_id": "job-frag-01",
                "generation": 1,
            }
        )
        self.engine.submit(
            {
                "action": "recover",
                "stage": "recover",
                "round_id": "frag-recover",
                "now_ms": 300,
                "reason": "normal_expiry",
            }
        )

    def setup_preemption_scene(self) -> None:
        for index in range(1, 6):
            self.grant(f"job-frag-{index:02d}", card="gpu-frag", round_id=f"pre-frag-{index}")
        self.grant("job-small-fill", card="gpu-small", round_id="small-fill")
        for suffix in ("a", "b", "c"):
            self.grant(f"job-prey-{suffix}", card="gpu-preempt", round_id=f"prey-{suffix}")
        self.engine.submit(
            {
                "action": "renew",
                "stage": "renew",
                "round_id": "pre-frag-renew",
                "now_ms": 150,
                "job_id": "job-frag-01",
                "generation": 1,
            }
        )
        self.engine.submit(
            {
                "action": "recover",
                "stage": "recover",
                "round_id": "pre-frag-recover",
                "now_ms": 300,
                "reason": "normal_expiry",
            }
        )


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        generator.main()
        cls.bundle = load_bundle(SAMPLES)

    def docs(self):
        return (
            json.loads((SAMPLES / "cards.json").read_text()),
            json.loads((SAMPLES / "jobs.json").read_text()),
            json.loads((SAMPLES / "config.json").read_text()),
            json.loads((SAMPLES / "inject.json").read_text()),
        )

    def test_sample_files_are_valid(self) -> None:
        for name in ("cards.json", "jobs.json", "config.json", "inject.json"):
            self.assertTrue((SAMPLES / name).is_file())
        self.assertEqual(len(self.bundle["cards"]), 6)
        self.assertIn("job-high-priority", self.bundle["jobs"])

    def test_duplicate_and_divisibility_errors(self) -> None:
        cards, jobs, config, inject = self.docs()
        bad_cards = json.loads(json.dumps(cards))
        bad_cards["cards"].append(dict(bad_cards["cards"][0]))
        with self.assertRaises(SchedulerError) as ctx:
            validate_bundle(bad_cards, jobs, config, inject)
        self.assertEqual(ctx.exception.error_code, "E_DUPLICATE_CARD_ID")

        bad_cards = json.loads(json.dumps(cards))
        bad_cards["cards"][0]["total_compute"] = 105
        bad_cards["cards"][0]["granularity"] = 10
        with self.assertRaises(SchedulerError) as ctx:
            validate_bundle(bad_cards, jobs, config, inject)
        self.assertEqual(ctx.exception.error_code, "E_GRANULARITY_DIVISIBILITY")

        bad_jobs = json.loads(json.dumps(jobs))
        bad_jobs["jobs"][0]["priority"] = "P9"
        with self.assertRaises(SchedulerError) as ctx:
            validate_bundle(cards, bad_jobs, config, inject)
        self.assertEqual(ctx.exception.error_code, "E_INVALID_PRIORITY")

        bad_inject = json.loads(json.dumps(inject))
        bad_inject["injections"][0]["target"]["card_id"] = "missing-card"
        with self.assertRaises(SchedulerError) as ctx:
            validate_bundle(cards, jobs, config, bad_inject)
        self.assertEqual(ctx.exception.error_code, "E_INJECTION_TARGET_INVALID")

    def test_invalid_cli_load_creates_no_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            samples = tmp_path / "samples"
            shutil.copytree(SAMPLES, samples)
            cards_path = samples / "cards.json"
            cards = json.loads(cards_path.read_text())
            cards["cards"].append(dict(cards["cards"][0]))
            cards_path.write_text(json.dumps(cards))
            db = tmp_path / "nested" / "should-not-exist.db"
            exit_code = cli_main(
                ["--samples", str(samples), "--db", str(db), "load"]
            )
            self.assertEqual(exit_code, 20)
            self.assertFalse(db.exists())


class GrantLifecycleTests(SchedulerTestBase):
    def test_deterministic_intervals_and_idempotent_repeat(self) -> None:
        first = self.grant("job-frag-01", card="gpu-frag")
        second = self.engine.submit(
            {
                "action": "grant",
                "stage": "grant",
                "round_id": "r-job-frag-01",
                "now_ms": 100,
                "job_id": "job-frag-01",
                "card_id": "gpu-frag",
            }
        )
        self.assertTrue(first["granted"])
        self.assertEqual(first["compute_interval"], [0, 20])
        self.assertEqual(first["memory_interval"], [0, 20])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["ledger_version"], second["ledger_version"])

    def test_quota_compute_is_first_trigger(self) -> None:
        for suffix in ("a", "b", "c"):
            self.grant(f"job-prey-{suffix}", card="gpu-preempt", round_id=f"prey-{suffix}")
        result = self.grant("job-quota-blocked", now=101)
        self.assertFalse(result["granted"])
        decision = result["decision"]
        self.assertEqual(decision["constraint"], "quota_compute")
        self.assertEqual(decision["used"], 100)
        self.assertEqual(decision["requested"], 40)
        self.assertEqual(decision["limit"], 120)

    def test_capacity_and_card_state_rejections(self) -> None:
        compute_big = self.grant("job-compute-too-big", now=101)
        self.assertEqual(compute_big["decision"]["constraint"], "card_compute")
        memory_big = self.grant("job-memory-too-big", now=102)
        self.assertEqual(memory_big["decision"]["constraint"], "card_memory")
        locked = self.grant("job-frag-01", card="gpu-locked", now=103)
        self.assertEqual(locked["decision"]["constraint"], "card_unsplittable")
        dead = self.grant("job-frag-02", card="gpu-dead", now=104)
        self.assertEqual(dead["decision"]["constraint"], "card_unhealthy")

    def test_renew_generation_fencing_and_expiry_recycle(self) -> None:
        self.grant("job-frag-02", card="gpu-frag")
        with self.assertRaises(SchedulerError) as ctx:
            self.engine.submit(
                {
                    "action": "renew",
                    "stage": "renew",
                    "round_id": "bad-gen",
                    "now_ms": 150,
                    "job_id": "job-frag-02",
                    "generation": 99,
                }
            )
        self.assertEqual(ctx.exception.error_code, "E_GENERATION_MISMATCH")

        renewed = self.engine.submit(
            {
                "action": "renew",
                "stage": "renew",
                "round_id": "good-renew",
                "now_ms": 150,
                "job_id": "job-frag-02",
                "generation": 1,
            }
        )
        self.assertEqual(renewed["generation"], 2)

        recover = self.engine.submit(
            {
                "action": "recover",
                "stage": "recover",
                "round_id": "recover-late",
                "now_ms": 400,
                "reason": "normal_expiry",
            }
        )
        self.assertEqual(recover["recycled_count"], 1)
        self.assertEqual(recover["records"][0]["reason"], "normal_expiry")
        status = self.engine.status()
        self.assertEqual(status["quota_groups"]["qg-frag"]["compute_used"], 0)

    def test_quota_shrink_recycles_with_distinct_reason(self) -> None:
        for index in range(1, 4):
            self.grant(f"job-frag-{index:02d}", card="gpu-frag", round_id=f"shrink-base-{index}")
        result = self.engine.submit(
            {
                "action": "recover",
                "stage": "recover",
                "round_id": "quota-shrink",
                "now_ms": 120,
                "reason": "quota_shrink",
                "quota_group": "qg-frag",
                "compute_limit": 40,
            }
        )
        self.assertGreaterEqual(result["recycled_count"], 1)
        self.assertTrue(all(r["reason"] == "quota_shrink" for r in result["records"]))
        self.assertLessEqual(self.engine.status()["quota_groups"]["qg-frag"]["compute_used"], 40)


class ReorganizationPreemptionTests(SchedulerTestBase):
    def test_fragment_report_requires_one_migration(self) -> None:
        self.setup_fragmented_card()
        result = self.engine.submit(
            {
                "action": "reorganize",
                "stage": "reorganize",
                "round_id": "reorg-test",
                "now_ms": 300,
                "job_id": "job-frag-target",
                "card_id": "gpu-frag",
            }
        )
        report = result["report"]
        self.assertEqual(report["conclusion"], "migration_required")
        self.assertEqual(report["minimum_migrations"], 1)
        self.assertTrue(report["migration_possible"])
        card_report = report["card_reports"][0]
        self.assertTrue(card_report["fragmentation_suspected"])
        self.assertFalse(card_report["true_no_remaining_capacity"])
        self.assertEqual([f["length"] for f in card_report["compute_fragments"]], [20, 20])

    def test_preemption_plan_and_execution_match(self) -> None:
        self.setup_preemption_scene()
        planned = self.engine.plan_preemption_now("job-high-priority", 300)["plan"]
        self.assertTrue(planned["feasible"])
        self.assertEqual(
            [self.engine.bundle and self.engine.status()["leases"][lid]["job_id"] for lid in planned["victim_lease_ids"]],
            ["job-prey-b", "job-prey-a"],
        )
        executed = self.engine.submit(
            {
                "action": "preempt_execute",
                "stage": "preempt",
                "round_id": "preempt-exec",
                "now_ms": 300,
                "job_id": "job-high-priority",
                "expected_plan_digest": planned["plan_digest"],
            }
        )
        self.assertEqual(executed["victim_lease_ids"], planned["victim_lease_ids"])
        self.assertEqual(executed["plan_digest"], planned["plan_digest"])
        self.assertTrue(executed["grant"]["granted"])
        self.assertEqual(executed["grant"]["compute_interval"], [0, 60])
        self.assertEqual(len(executed["refunds"]), 2)
        self.assertTrue(all(item["status"] == "open" for item in executed["refunds"]))
        invariants = self.engine.verify_current_invariants()
        self.assertTrue(invariants["ok"], invariants["errors"])


class InjectionTests(SchedulerTestBase):
    def test_all_four_injection_codes_are_isolated(self) -> None:
        with self.assertRaises(SchedulerError) as ctx:
            self.engine.submit(
                {
                    "action": "grant",
                    "stage": "grant",
                    "round_id": "inj-grant-overlap",
                    "now_ms": 10,
                    "job_id": "job-inj-overlap",
                    "card_id": "gpu-frag",
                }
            )
        self.assertEqual(ctx.exception.error_code, "E_INJECT_INTERVAL_OVERLAP")

        self.grant("job-frag-01", card="gpu-frag")
        with self.assertRaises(SchedulerError) as ctx:
            self.engine.submit(
                {
                    "action": "renew",
                    "stage": "renew",
                    "round_id": "inj-renew-rollback",
                    "now_ms": 150,
                    "job_id": "job-frag-01",
                    "generation": 1,
                }
            )
        self.assertEqual(ctx.exception.error_code, "E_INJECT_GENERATION_ROLLBACK")

        with self.assertRaises(SchedulerError) as ctx:
            self.engine.submit(
                {
                    "action": "recover",
                    "stage": "recover",
                    "round_id": "inj-recover-imbalance",
                    "now_ms": 200,
                    "reason": "normal_expiry",
                }
            )
        self.assertEqual(ctx.exception.error_code, "E_INJECT_LEDGER_IMBALANCE")

        with self.assertRaises(SchedulerError) as ctx:
            self.engine.submit(
                {
                    "action": "reorganize",
                    "stage": "reorganize",
                    "round_id": "inj-reorg-mismatch",
                    "now_ms": 200,
                    "job_id": "job-frag-target",
                    "card_id": "gpu-frag",
                }
            )
        self.assertEqual(ctx.exception.error_code, "E_INJECT_REORG_DIGEST_MISMATCH")

        check = verify_injection_isolation(self.engine.store, self.bundle)
        self.assertTrue(check["ok"], check["errors"])
        status = self.engine.status()
        self.assertEqual(status["ledger_version"], 2)  # load + one successful base grant


class CrashRecoverySubprocessTests(unittest.TestCase):
    def run_cli(self, db: Path, *args: str, crash_at: str | None = None, check: bool = True):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        if crash_at:
            env["GPU_SCHEDULER_CRASH_AT"] = crash_at
        result = subprocess.run(
            [sys.executable, "-m", "gpu_scheduler.cli", "--db", str(db), *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"command failed {args}: {result.stderr}\n{result.stdout}")
        return result

    def test_grant_all_crash_points_recover_cleanly(self) -> None:
        for crash_at, expected_mode in (
            ("grant:after_intent", "replayed_planned_operation"),
            ("grant:before_commit", "replayed_planned_operation"),
            ("grant:after_commit", "finalized_applied_commit"),
        ):
            with self.subTest(crash_at=crash_at):
                with tempfile.TemporaryDirectory() as tmp:
                    db = Path(tmp) / "crash.db"
                    self.run_cli(db, "load")
                    crashed = self.run_cli(
                        db,
                        "grant",
                        "--round",
                        "crash-grant",
                        "--now",
                        "100",
                        "--job",
                        "job-frag-01",
                        "--card",
                        "gpu-frag",
                        crash_at=crash_at,
                        check=False,
                    )
                    self.assertEqual(crashed.returncode, -9)
                    resumed = json.loads(self.run_cli(db, "resume").stdout)
                    self.assertEqual(resumed["recovered_count"], 1)
                    self.assertEqual(resumed["recovered"][0]["mode"], expected_mode)
                    status = json.loads(self.run_cli(db, "status").stdout)
                    self.assertEqual(len(status["leases"]), 1)
                    self.assertEqual(status["pending_operations"], [])

    def test_each_lifecycle_stage_survives_kill(self) -> None:
        stages = ("renew", "recover", "reorganize")
        for stage in stages:
            for point in ("before_commit", "after_commit"):
                crash_at = f"{stage}:{point}"
                with self.subTest(crash_at=crash_at):
                    with tempfile.TemporaryDirectory() as tmp:
                        db = Path(tmp) / "stage.db"
                        self.run_cli(db, "load")
                        self.run_cli(
                            db, "grant", "--round", "g1", "--now", "100",
                            "--job", "job-frag-02", "--card", "gpu-frag",
                        )
                        args = {
                            "renew": [
                                "renew", "--round", "r1", "--now", "150",
                                "--job", "job-frag-02", "--generation", "1",
                            ],
                            "recover": [
                                "recover", "--round", "rc1", "--now", "300",
                            ],
                            "reorganize": [
                                "reorganize", "--round", "ro1", "--now", "300",
                                "--job", "job-frag-target", "--card", "gpu-frag",
                            ],
                        }[stage]
                        crashed = self.run_cli(db, *args, crash_at=crash_at, check=False)
                        self.assertEqual(crashed.returncode, -9, crashed.stderr)
                        resumed = json.loads(self.run_cli(db, "resume").stdout)
                        self.assertGreaterEqual(resumed["recovered_count"], 1)
                        status = json.loads(self.run_cli(db, "status").stdout)
                        self.assertEqual(status["pending_operations"], [])
                        invariants = SchedulerEngine(
                            SchedulerStore(db), load_bundle(SAMPLES)
                        ).verify_current_invariants()
                        self.assertTrue(invariants["ok"], invariants["errors"])


class MultiprocessConcurrencyTests(unittest.TestCase):
    def test_parallel_same_batch_has_no_oversell_or_duplicate_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mp.db"
            env_base = dict(os.environ, PYTHONPATH=str(ROOT))
            subprocess.run(
                [sys.executable, "-m", "gpu_scheduler.cli", "--db", str(db), "load"],
                cwd=ROOT, env=env_base, check=True, capture_output=True, text=True,
            )
            duplicate_commands = [
                [sys.executable, "-m", "gpu_scheduler.cli", "--db", str(db),
                 "grant", "--round", "mp-shared", "--now", "100",
                 "--job", "job-mp-01", "--card", "gpu-mp"]
                for _ in range(4)
            ]
            duplicate_procs = [
                subprocess.Popen(cmd, cwd=ROOT, env=env_base, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for cmd in duplicate_commands
            ]
            job_01 = []
            for proc in duplicate_procs:
                stdout, stderr = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0, stderr)
                job_01.append(json.loads(stdout))
            self.assertEqual(len(job_01), 4)
            self.assertEqual(len({item["lease_id"] for item in job_01}), 1)

            distinct_commands = [
                [sys.executable, "-m", "gpu_scheduler.cli", "--db", str(db),
                 "grant", "--round", "mp-distinct", "--now", "100",
                 "--job", f"job-mp-{index:02d}", "--card", "gpu-mp"]
                for index in range(2, 7)
            ]
            distinct_procs = [
                subprocess.Popen(cmd, cwd=ROOT, env=env_base, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for cmd in distinct_commands
            ]
            for proc in distinct_procs:
                stdout, stderr = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0, stderr)

            status = json.loads(
                subprocess.run(
                    [sys.executable, "-m", "gpu_scheduler.cli", "--db", str(db), "status"],
                    cwd=ROOT, env=env_base, check=True, capture_output=True, text=True,
                ).stdout
            )
            mp_card = status["cards"]["gpu-mp"]
            self.assertLessEqual(mp_card["used_compute"], 40)
            self.assertLessEqual(mp_card["used_memory"], 40)
            active_mp = [
                lease for lease in status["leases"].values()
                if lease["card_id"] == "gpu-mp" and lease["status"] == "active"
            ]
            self.assertEqual(len(active_mp), 2)
            self.assertEqual(
                sum(1 for lease in active_mp if lease["job_id"] == "job-mp-01"), 1
            )
            engine = SchedulerEngine(SchedulerStore(db), load_bundle(SAMPLES))
            self.assertTrue(engine.verify_current_invariants()["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
