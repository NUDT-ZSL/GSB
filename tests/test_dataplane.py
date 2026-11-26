from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DataPlaneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dp-test-")
        self.data = Path(self.tmp.name) / "data"
        self.env = {**os.environ, "PYTHONPATH": str(ROOT)}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cli(self, *args: str, check: bool = True, env=None):
        merged_env = {**self.env, **(env or {})}
        proc = subprocess.run(
            [sys.executable, "-m", "dataplane", *args],
            cwd=ROOT,
            env=merged_env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if check and proc.returncode != 0:
            self.fail(
                f"command failed: {' '.join(args)}\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = {"raw_stdout": proc.stdout, "stderr": proc.stderr}
        return proc, payload

    def seed_and_load(self) -> None:
        self.cli("seed", "--data-dir", str(self.data), "--clock-ms", "1000")
        self.cli("load", "--data-dir", str(self.data), "--clock-ms", "1000")

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.data / "state.sqlite3")
        conn.row_factory = sqlite3.Row
        return conn

    def test_end_to_end_patch_adjudication_and_verify(self) -> None:
        self.seed_and_load()
        _, generated = self.cli(
            "generate",
            "--data-dir",
            str(self.data),
            "--clock-ms",
            "2000",
            "--budget-ms",
            "100000",
            "--concurrency",
            "4",
        )
        self.assertEqual(generated["status"]["task_states"]["committed"], 26)

        with self.db() as conn:
            before = dict(
                conn.execute(
                    "SELECT content_digest, decision_id FROM block_task "
                    "WHERE case_id='case-007' AND block_index=0 AND state='committed'"
                ).fetchone()
            )
            case7_count = conn.execute(
                "SELECT COUNT(*) AS n FROM block_task WHERE case_id='case-007'"
            ).fetchone()["n"]

        _, patched = self.cli(
            "patch",
            "--data-dir",
            str(self.data),
            "--clock-ms",
            "3000",
            "--budget-ms",
            "100000",
            "--concurrency",
            "4",
        )
        affected = {
            item["case_id"]: item["affected_blocks"]
            for item in patched["closure"]["closures"]
        }
        self.assertEqual(affected["case-001"], [0, 1, 2])
        self.assertEqual(affected["case-005"], [0, 1, 2, 3])
        self.assertEqual(affected["case-007"], [])
        self.assertEqual(patched["schedule"]["failed_count"], 0)

        _, patched_again = self.cli(
            "patch", "--data-dir", str(self.data), "--clock-ms", "3000"
        )
        self.assertEqual(
            sum(t["created"] for t in patched_again["closure"]["planned_tasks"]), 0
        )

        with self.db() as conn:
            after = dict(
                conn.execute(
                    "SELECT content_digest, decision_id FROM block_task "
                    "WHERE case_id='case-007' AND block_index=0 AND state='committed'"
                ).fetchone()
            )
            self.assertEqual(case7_count, 4)
            sensitive = conn.execute(
                "SELECT COUNT(*) AS n FROM inference_decision WHERE precision_sensitive=1"
            ).fetchone()["n"]
            self.assertGreater(sensitive, 0)
            chain = conn.execute(
                "SELECT input_chain_json, quantization_json FROM inference_decision LIMIT 1"
            ).fetchone()
            self.assertIn("effective_patches", chain["input_chain_json"])
            self.assertIn("fixed_point_scale", chain["quantization_json"])

        self.assertEqual(before, after)
        proc, verify = self.cli(
            "verify", "--data-dir", str(self.data), "--clock-ms", "3100", "--strict"
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(verify["ok"])
        self.assertTrue(verify["complete"])
        self.assertEqual(verify["issues"], [])
        self.assertGreater(verify["batch"]["precision_sensitive_count"], 0)

    def test_scheduler_outcome_categories(self) -> None:
        self.seed_and_load()
        _, report = self.cli(
            "generate",
            "--data-dir",
            str(self.data),
            "--clock-ms",
            "2000",
            "--budget-ms",
            "100000",
            "--concurrency",
            "1",
            "--max-rounds",
            "1",
        )
        round_report = report["schedule"]["rounds"][0]
        self.assertEqual(round_report["completed_count"], 1)
        self.assertGreater(round_report["not_started_count"], 0)
        self.assertEqual(round_report["unselected"][0]["reason"], "concurrency_limit")
        self.assertEqual(
            round_report["unselected"][0]["budget_state"], "within_budget"
        )

        _, budget = self.cli(
            "generate",
            "--data-dir",
            str(self.data),
            "--clock-ms",
            "2001",
            "--budget-ms",
            "2",
            "--concurrency",
            "10",
            "--max-rounds",
            "1",
        )
        round_report = budget["schedule"]["rounds"][0]
        self.assertEqual(round_report["completed_count"], 1)
        self.assertGreater(round_report["abandoned_count"], 0)
        self.assertEqual(round_report["unselected"][0]["reason"], "budget_exhausted")

    def test_failure_then_resume_does_not_duplicate_ledger(self) -> None:
        self.seed_and_load()
        _, failed = self.cli(
            "generate",
            "--data-dir",
            str(self.data),
            "--clock-ms",
            "2000",
            "--concurrency",
            "1",
            env={"DP_FAIL_AT": "case-001:0"},
        )
        self.assertEqual(failed["schedule"]["failed_count"], 1)
        with self.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM block_task WHERE state='failed'").fetchone()["n"],
                1,
            )

        self.cli(
            "resume",
            "--data-dir",
            str(self.data),
            "--clock-ms",
            "2100",
            "--concurrency",
            "1",
        )
        with self.db() as conn:
            decisions = conn.execute("SELECT COUNT(*) AS n FROM inference_decision").fetchone()["n"]
            ledger = conn.execute("SELECT COUNT(*) AS n FROM error_ledger").fetchone()["n"]
        self.cli(
            "resume", "--data-dir", str(self.data), "--clock-ms", "2200", "--concurrency", "1"
        )
        with self.db() as conn:
            decisions2 = conn.execute("SELECT COUNT(*) AS n FROM inference_decision").fetchone()["n"]
            ledger2 = conn.execute("SELECT COUNT(*) AS n FROM error_ledger").fetchone()["n"]
        self.assertEqual((decisions, ledger), (26, 26))
        self.assertEqual((decisions2, ledger2), (26, 26))
        _, verify = self.cli(
            "verify", "--data-dir", str(self.data), "--clock-ms", "2300", "--strict"
        )
        self.assertTrue(verify["ok"])

    def test_kill_after_materialized_commit_recovers_half_block(self) -> None:
        self.seed_and_load()
        proc, _ = self.cli(
            "generate",
            "--data-dir",
            str(self.data),
            "--clock-ms",
            "2000",
            "--concurrency",
            "4",
            env={"DP_CRASH_AT": "materialized_committed:case-001:0"},
            check=False,
        )
        self.assertEqual(proc.returncode, 97)
        with self.db() as conn:
            states = {
                row["state"]: row["n"]
                for row in conn.execute("SELECT state, COUNT(*) n FROM block_task GROUP BY state")
            }
            self.assertIn("materialized", states)

        _, resumed = self.cli(
            "resume", "--data-dir", str(self.data), "--clock-ms", "2100", "--concurrency", "4"
        )
        self.assertTrue(
            any(item["action"] == "finalized_materialized" for item in resumed["recovery"])
        )
        self.cli("resume", "--data-dir", str(self.data), "--clock-ms", "2200")
        _, verify = self.cli(
            "verify", "--data-dir", str(self.data), "--clock-ms", "2300", "--strict"
        )
        self.assertTrue(verify["ok"], verify.get("issues"))

    def test_config_evolution_and_crash_recovery(self) -> None:
        self.seed_and_load()
        proc, _ = self.cli(
            "reconfigure",
            "--data-dir",
            str(self.data),
            "--package",
            str(self.data / "config_v2_incremental"),
            "--clock-ms",
            "4000",
            env={"DP_CRASH_AT": "config_staged:2026112703"},
            check=False,
        )
        self.assertEqual(proc.returncode, 97)
        _, resumed = self.cli(
            "resume", "--data-dir", str(self.data), "--clock-ms", "4100"
        )
        self.assertEqual(
            resumed["pending_config"]["classification"], "incremental_append"
        )
        _, noop = self.cli(
            "reconfigure",
            "--data-dir",
            str(self.data),
            "--package",
            str(self.data / "config_v2_incremental"),
            "--clock-ms",
            "4200",
        )
        self.assertEqual(noop["config"]["classification"], "noop")

        proc, rollback = self.cli(
            "reconfigure",
            "--data-dir",
            str(self.data),
            "--package",
            str(self.data / "config_v2"),
            "--clock-ms",
            "4300",
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(rollback["error_code"], "E_CONFIG_VERSION_ROLLBACK")

        self.cli(
            "generate",
            "--data-dir",
            str(self.data),
            "--clock-ms",
            "4400",
            "--budget-ms",
            "100000",
        )
        _, verify = self.cli(
            "verify", "--data-dir", str(self.data), "--clock-ms", "4500", "--strict"
        )
        self.assertTrue(verify["ok"], verify.get("issues"))
        self.assertEqual(verify["batch"]["config_version"], 2026112703)
        self.assertEqual(verify["batch"]["decision_count"], 26)

    def test_multiprocess_same_case_creates_one_ledger_set(self) -> None:
        self.seed_and_load()
        processes = []
        for _ in range(4):
            log = self.tmp.name + "/mp.json"
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "dataplane",
                        "generate",
                        "--data-dir",
                        str(self.data),
                        "--clock-ms",
                        "2000",
                        "--case-id",
                        "case-001",
                    ],
                    cwd=ROOT,
                    env=self.env,
                    stdout=open(log, "w"),
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
        for process in processes:
            self.assertEqual(process.wait(timeout=60), 0)
        with self.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM block_task").fetchone()["n"], 3
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM inference_decision").fetchone()["n"], 3
            )

    def test_business_error_codes(self) -> None:
        self.cli("seed", "--data-dir", str(self.data))

        tasks_path = self.data / "tasks.jsonl"
        rows = [json.loads(line) for line in tasks_path.read_text().splitlines()]
        bad = list(rows)
        bad[0] = dict(bad[0], data_size=-3)
        tasks_path.write_text("\n".join(json.dumps(r) for r in bad) + "\n")
        proc, payload = self.cli("load", "--data-dir", str(self.data), check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(payload["error_code"], "E_DATA_SIZE_INVALID")

        shutil.rmtree(self.data)
        self.cli("seed", "--data-dir", str(self.data))
        sample_path = self.data / "samples" / "case-001.json"
        sample = json.loads(sample_path.read_text())
        sample["tensors"][0]["digest"] = "sha256:" + "0" * 64
        sample_path.write_text(json.dumps(sample))
        proc, payload = self.cli("load", "--data-dir", str(self.data), check=False)
        self.assertEqual(payload["error_code"], "E_DIGEST_MISMATCH")

        shutil.rmtree(self.data)
        self.cli("seed", "--data-dir", str(self.data))
        patch_path = self.data / "patches.jsonl"
        rows = [json.loads(line) for line in patch_path.read_text().splitlines()]
        rows[0]["block_range"] = [0, 999]
        patch_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        proc, payload = self.cli("load", "--data-dir", str(self.data), check=False)
        self.assertEqual(payload["error_code"], "E_BLOCK_RANGE_OUT_OF_BOUNDS")
        self.assertIn("block_count", payload["details"])

        shutil.rmtree(self.data)
        self.cli("seed", "--data-dir", str(self.data))
        v1_path = self.data / "config_v1.json"
        v1 = json.loads(v1_path.read_text())
        v1["effective_after_seconds"] = -1
        v1_path.write_text(json.dumps(v1))
        proc, payload = self.cli("load", "--data-dir", str(self.data), check=False)
        self.assertEqual(payload["error_code"], "E_V1_EFFECTIVE_SECONDS_INVALID")

        shutil.rmtree(self.data)
        self.cli("seed", "--data-dir", str(self.data))
        manifest_path = self.data / "config_v2" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["shards"][0]["sha256"] = "sha256:" + "f" * 64
        manifest_path.write_text(json.dumps(manifest))
        proc, payload = self.cli("load", "--data-dir", str(self.data), check=False)
        self.assertEqual(payload["error_code"], "E_CONFIG_SHARD_HASH_MISMATCH")
        self.assertIn("actual_sha256", payload["details"])

    def test_clock_skew_and_patch_generation_rollback(self) -> None:
        self.seed_and_load()
        self.cli("generate", "--data-dir", str(self.data), "--clock-ms", "10000")
        proc, payload = self.cli(
            "generate", "--data-dir", str(self.data), "--clock-ms", "100", check=False
        )
        self.assertEqual(payload["error_code"], "E_CLOCK_ROLLBACK_TOLERANCE")
        self.assertIn("rollback_ms", payload["details"])

        self.cli(
            "patch", "--data-dir", str(self.data), "--clock-ms", "11000"
        )
        bad_patch = self.tmp.name + "/bad-patch.json"
        Path(bad_patch).write_text(
            json.dumps(
                {
                    "patch_id": "patch-old-gen",
                    "case_id": "case-001",
                    "block_range": [0, 1],
                    "patch_kind": "weight_update",
                    "patch_generation": 1,
                    "logical_clock_ms": 12000,
                    "new_digest": "sha256:" + "0" * 64,
                }
            )
        )
        proc, payload = self.cli(
            "patch",
            "--data-dir",
            str(self.data),
            "--clock-ms",
            "12000",
            "--file",
            bad_patch,
            check=False,
        )
        self.assertEqual(payload["error_code"], "E_PATCH_GENERATION_ROLLBACK")


if __name__ == "__main__":
    unittest.main()
