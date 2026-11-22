from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from config_center.errors import BusinessError
from config_center.service import ConfigCenter
from config_center.util import sha256_json
from config_center.verify import verify_center
from config_center.web import create_app


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PYTHON = sys.executable


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.center = ConfigCenter(self.root, data_dir=DATA)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cli(
        self,
        *args: str,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [
                PYTHON,
                "-m",
                "config_center",
                "--root",
                str(self.root),
                "--data-dir",
                str(DATA),
                *args,
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def assert_business_error(self, code: str, callback) -> BusinessError:
        with self.assertRaises(BusinessError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_invalid_inputs_do_not_write_business_state(self) -> None:
        self.assert_business_error(
            "E_BAD_CLOCK",
            lambda: self.center.load_file(DATA / "error_cases" / "bad_clock.json"),
        )
        self.assert_business_error(
            "E_BAD_CONFIG",
            lambda: self.center.load_file(DATA / "error_cases" / "bad_type.json"),
        )
        self.assert_business_error(
            "E_DIGEST_MISMATCH",
            lambda: self.center.apply_file(
                DATA / "error_cases" / "bad_digest_patch.json"
            ),
        )
        status = self.center.status()
        self.assertEqual(status["versions"], [])
        self.assertEqual(status["pointers"], [])

    def test_duplicate_json_keys_are_rejected(self) -> None:
        from config_center.util import DuplicateKeyError, strict_loads

        with self.assertRaises(DuplicateKeyError):
            strict_loads('{"configs":{"a":1,"a":2}}')

    def test_load_is_idempotent_and_version_rules_hold(self) -> None:
        first = self.center.load_file(DATA / "snapshot.json")
        self.assertEqual(first["status"], "loaded")
        second = self.center.load_file(DATA / "snapshot.json")
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["status"], "noop")
        self.assertEqual(len(self.center.store.list_versions("edge-pipeline")), 1)

        conflict_patch = {
            "namespace": "edge-pipeline",
            "base_version": 1,
            "target_version": 1,
            "logical_clock_ms": 1100,
            "mode": "merge",
            "operations": [
                {
                    "op": "set",
                    "key": "timeout_ms",
                    "value": 1300,
                }
            ],
        }
        conflict_patch["content_digest"] = sha256_json(conflict_patch)
        self.assert_business_error(
            "E_VERSION_CONFLICT",
            lambda: self.center.apply_patch_document(conflict_patch),
        )

    def test_incremental_and_full_replace_with_residual_carry(self) -> None:
        self.center.load_file(DATA / "snapshot.json")
        patch2 = self.center.apply_file(
            DATA / "patches" / "002_add_weight_gamma.json"
        )
        self.assertEqual(patch2["change_kind"], "incremental_append")
        patch3 = self.center.apply_file(
            DATA / "patches" / "003_full_replace_same_values.json"
        )
        self.assertEqual(patch3["change_kind"], "full_replace")

        v1 = self.center.store.read_version_bundle("edge-pipeline", 1)
        v2 = self.center.store.read_version_bundle("edge-pipeline", 2)
        v3 = self.center.store.read_version_bundle("edge-pipeline", 3)
        self.assertEqual(v1.configs["filter_threshold"].fixed_int, 1350)
        self.assertEqual(v1.configs["filter_threshold"].residual_units, Decimal("0.5"))
        self.assertEqual(v2.configs["filter_threshold"].fixed_int, 1350)
        self.assertEqual(v3.configs["filter_threshold"].fixed_int, 1351)
        self.assertEqual(v3.configs["filter_threshold"].residual_units, Decimal("0"))
        self.assertNotEqual(
            v1.configs["filter_threshold"].conversion_id,
            v3.configs["filter_threshold"].conversion_id,
        )

        with self.center.store.read_session() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM conversion_ledger"
            ).fetchone()[0]
            self.center.store.read_bundle(conn, "edge-pipeline", 3)
            self.center.store.read_bundle(conn, "edge-pipeline", 3)
            after = conn.execute(
                "SELECT COUNT(*) FROM conversion_ledger"
            ).fetchone()[0]
        self.assertEqual(before, after)

    def test_rollback_rejected(self) -> None:
        self.center.load_file(DATA / "snapshot.json")
        self.center.apply_file(DATA / "patches" / "002_add_weight_gamma.json")
        self.center.apply_file(DATA / "patches" / "003_full_replace_same_values.json")
        self.assert_business_error(
            "E_VERSION_ROLLBACK",
            lambda: self.center.apply_file(
                DATA / "patches" / "002_add_weight_gamma.json"
            ),
        )

    def test_duplicate_patch_is_idempotent(self) -> None:
        self.center.load_file(DATA / "snapshot.json")
        first = self.center.apply_file(DATA / "patches" / "002_add_weight_gamma.json")
        second = self.center.apply_file(DATA / "patches" / "002_add_weight_gamma.json")
        self.assertEqual(first["version"], 2)
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["change_kind"], "noop")
        self.assertEqual(len(self.center.store.list_versions("edge-pipeline")), 2)

    def test_same_round_different_input_is_rejected(self) -> None:
        self.center.load_file(DATA / "snapshot.json")
        sample = json.loads((DATA / "compute_input.json").read_text(encoding="utf-8"))
        self.center.compute_document(sample)
        changed = json.loads(json.dumps(sample))
        changed["events"][0]["weight"] = "2.75000"
        self.assert_business_error(
            "E_ROUND_CONFLICT",
            lambda: self.center.compute_document(changed),
        )
        status = self.center.status("edge-pipeline")
        self.assertEqual(
            sum(len(client["rounds"]) for client in status["clients"]), 1
        )

    def test_numeric_diagnostics_and_high_scale_flip(self) -> None:
        self.center.bootstrap_demo()
        comparison = self.center.compare_versions(
            "edge-pipeline",
            1,
            3,
            json.loads((DATA / "compute_input.json").read_text(encoding="utf-8")),
            tolerance=Decimal("0.001"),
            scale_factor=100,
        )
        self.assertTrue(comparison["comparison"]["precision_sensitive"])
        threshold = next(
            row
            for row in comparison["comparison"]["metrics"]
            if row["metric"] == "threshold"
        )
        self.assertEqual(threshold["fixed_delta"], "-0.0002")
        self.assertEqual(threshold["exact_delta"], "0")
        self.assertTrue(threshold["conclusion_changed"])
        self.assertTrue(threshold["precision_sensitive"])
        self.assertTrue(comparison["higher_scale"]["flips_conclusion"])

        compute = self.center.compute_file(DATA / "compute_input.json")
        for name, metric in compute["result"]["metrics"].items():
            self.assertTrue(metric["bound_ok"], f"{name} exceeds error bound")
        self.assertTrue(comparison["left"]["sensitivity"]["precision_sensitive"])

    def test_verify_passes(self) -> None:
        self.center.bootstrap_demo()
        result = verify_center(
            self.center,
            namespace="edge-pipeline",
            tolerance=Decimal("0.001"),
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["summary"]["failed"], 0)
        self.assertGreaterEqual(result["summary"]["checks"], 7)

    def test_crash_recovery_replays_only_uncommitted_bound_version(self) -> None:
        self.center.load_file(DATA / "snapshot.json")
        crashed = self.cli(
            "compute",
            str(DATA / "compute_input.json"),
            "--round-key",
            "round-crash",
            "--crash-at",
            "before-commit",
        )
        self.assertEqual(crashed.returncode, 86, crashed.stderr)
        status = self.center.status("edge-pipeline")
        self.assertEqual(len(status["pending_jobs"]), 1)
        self.assertEqual(status["pending_jobs"][0]["bound_version"], 1)
        self.assertEqual(
            sum(len(client["rounds"]) for client in status["clients"]), 0
        )

        self.center.apply_file(DATA / "patches" / "002_add_weight_gamma.json")
        self.center.apply_file(DATA / "patches" / "003_full_replace_same_values.json")
        resumed = self.cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        payload = json.loads(resumed.stdout)
        self.assertEqual(payload["resumed_count"], 1)
        self.assertEqual(payload["resumed"][0]["bound_version"], 1)

        repeated = self.cli(
            "compute",
            str(DATA / "compute_input.json"),
            "--round-key",
            "round-crash",
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        repeated_payload = json.loads(repeated.stdout)
        self.assertTrue(repeated_payload["idempotent"])
        self.assertEqual(repeated_payload["bound_version"], 1)
        status = self.center.status("edge-pipeline")
        self.assertEqual(len(status["pending_jobs"]), 0)
        self.assertEqual(
            sum(len(client["rounds"]) for client in status["clients"]), 1
        )

    def test_midflight_update_keeps_bound_version(self) -> None:
        self.center.load_file(DATA / "snapshot.json")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        process = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "config_center",
                "--root",
                str(self.root),
                "--data-dir",
                str(DATA),
                "compute",
                str(DATA / "compute_input.json"),
                "--round-key",
                "round-hot",
                "--delay-ms",
                "900",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 4
        while time.time() < deadline:
            pending = self.center.store.list_pending_jobs("edge-pipeline")
            if any(job["round_key"] == "round-hot" for job in pending):
                break
            time.sleep(0.05)
        else:
            process.kill()
            self.fail("compute process did not bind a version in time")
        applied = self.center.apply_file(
            DATA / "patches" / "002_add_weight_gamma.json"
        )
        self.assertEqual(applied["version"], 2)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["bound_version"], 1)
        self.assertEqual(payload["result"]["bound_version"], 1)

    def test_concurrent_apply_and_compute_are_idempotent(self) -> None:
        self.center.load_file(DATA / "snapshot.json")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        commands = [
            [
                PYTHON,
                "-m",
                "config_center",
                "--root",
                str(self.root),
                "--data-dir",
                str(DATA),
                "apply",
                str(DATA / "patches" / "002_add_weight_gamma.json"),
            ]
            for _ in range(6)
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for command in commands
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            json.loads(stdout)
        versions = self.center.store.list_versions("edge-pipeline")
        self.assertEqual([item["version"] for item in versions], [1, 2])

        self.center.apply_file(DATA / "patches" / "003_full_replace_same_values.json")
        compute_commands = [
            [
                PYTHON,
                "-m",
                "config_center",
                "--root",
                str(self.root),
                "--data-dir",
                str(DATA),
                "compute",
                str(DATA / "compute_input.json"),
                "--round-key",
                "race-round",
            ]
            for _ in range(6)
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for command in compute_commands
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            json.loads(stdout)
        status = self.center.status("edge-pipeline")
        rounds = [
            round_item
            for client in status["clients"]
            for round_item in client["rounds"]
        ]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["round_key"], "race-round")
        stored = self.center.store.read_result_full(
            "edge-pipeline", "worker-a", "race-round"
        )
        self.assertIsNotNone(stored)
        with self.center.store.read_session() as conn:
            result_count = conn.execute(
                "SELECT COUNT(*) FROM client_results WHERE round_key='race-round'"
            ).fetchone()[0]
            ledger_count = conn.execute(
                """
                SELECT COUNT(*) FROM error_ledger
                WHERE result_id IN (
                    SELECT result_id FROM client_results WHERE round_key='race-round'
                )
                """
            ).fetchone()[0]
        self.assertEqual(result_count, 1)
        self.assertEqual(ledger_count, len(stored["error_ledger"]))
        self.assertGreater(ledger_count, 0)

    def test_status_cli_is_stably_sorted(self) -> None:
        self.center.bootstrap_demo()
        first = self.cli("status", "--json")
        second = self.cli("status", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertEqual(payload["current_version"], 3)
        self.assertIn("logical_clock_watermark", payload)

    def test_http_api_smoke(self) -> None:
        app = create_app(self.root, DATA)
        client = app.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        boot = client.post("/api/bootstrap", json={"confirm": "BOOTSTRAP"})
        self.assertEqual(boot.status_code, 200, boot.get_data(as_text=True))
        compute = client.post(
            "/api/compute",
            json=json.loads((DATA / "compute_input.json").read_text(encoding="utf-8")),
        )
        self.assertEqual(compute.status_code, 200, compute.get_data(as_text=True))
        self.assertEqual(compute.json["bound_version"], 3)
        compare = client.post(
            "/api/compare",
            json={
                "namespace": "edge-pipeline",
                "from_version": 1,
                "to_version": 3,
                "input": json.loads(
                    (DATA / "compute_input.json").read_text(encoding="utf-8")
                ),
                "tolerance": "0.001",
                "scale_factor": 100,
            },
        )
        self.assertEqual(compare.status_code, 200, compare.get_data(as_text=True))
        self.assertTrue(compare.json["comparison"]["precision_sensitive"])
        verify = client.get("/api/verify?namespace=edge-pipeline")
        self.assertEqual(verify.status_code, 200)
        self.assertTrue(verify.json["passed"])


if __name__ == "__main__":
    unittest.main()

