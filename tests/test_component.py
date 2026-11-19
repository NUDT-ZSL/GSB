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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replication_diag.config import load_config
from replication_diag.injections import run_injection_manifest
from replication_diag.lag import compute_lag
from replication_diag.merge import merge_records, selected_migrations
from replication_diag.pipeline import load_sources
from replication_diag.util import stable_json_text

CONFIG = ROOT / "config" / "sample.json"
INJECTIONS = ROOT / "config" / "injections.json"
FULL_PACKAGE = ROOT / "samples" / "packages" / "full-2.2.0" / "manifest.json"
INCREMENTAL_PACKAGE = (
    ROOT / "samples" / "packages" / "incremental-2.1.0" / "manifest.json"
)
POSITION_ROLLBACK_PACKAGE = (
    ROOT / "samples" / "packages" / "rollback-1.9.0" / "manifest.json"
)
VERSION_ROLLBACK_PACKAGE = (
    ROOT / "samples" / "packages" / "version-rollback-1.9.0" / "manifest.json"
)


def clean_state(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", ".lock"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "replication_diag", *args],
        cwd=ROOT,
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def parse_json_output(process: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(process.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"invalid JSON output: {process.stdout!r}; stderr={process.stderr!r}"
        ) from exc


class FixtureAndMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(CONFIG)
        cls.bundle = load_sources(cls.config)
        cls.merged = merge_records(
            cls.bundle.v1_records, cls.bundle.v2_records, cls.config
        )

    def test_overlap_arbitration_and_stable_order(self) -> None:
        by_tx = {record.tx_num: record for record in self.merged}
        self.assertEqual([record.tx_num for record in self.merged], list(range(1, 13)))
        self.assertEqual(by_tx[1].source_version, "v1")
        self.assertEqual(by_tx[5].source_version, "v2")
        self.assertEqual(by_tx[6].source_version, "v2")
        self.assertEqual(by_tx[6].replay_generation, 1)
        self.assertEqual(by_tx[8].source_version, "v2")
        self.assertTrue(by_tx[8].committed)
        self.assertEqual(by_tx[10].tombstone_applied, True)

        reversed_v2 = list(reversed(self.bundle.v2_records))
        alternate = merge_records(self.bundle.v1_records, reversed_v2, self.config)
        self.assertEqual(
            stable_json_text([record.to_json() for record in alternate]),
            stable_json_text([record.to_json() for record in self.merged]),
        )
        swapped_arguments = merge_records(
            self.bundle.v2_records, self.bundle.v1_records, self.config
        )
        self.assertEqual(
            stable_json_text([record.to_json() for record in swapped_arguments]),
            stable_json_text([record.to_json() for record in self.merged]),
        )

    def test_migration_preserves_original_representations(self) -> None:
        migrations = selected_migrations(self.merged)
        self.assertEqual([item["tx_id"] for item in migrations], [
            f"{number:020d}" for number in range(1, 5)
        ])
        first = self.merged[0]
        self.assertEqual(first.original_delay["kind"], "v1_relative_delay_ms")
        self.assertEqual(first.original_delay["value"], 5)
        self.assertEqual(first.migration["scale"], "1/1")
        self.assertEqual(first.migration["rounding_direction"], "exact")
        self.assertEqual(first.migration["replay_generation"]["to"], 0)
        fifth = self.merged[4]
        self.assertEqual(fifth.original_time["kind"], "absolute_ms")
        self.assertEqual(fifth.original_delay["value"], 7)

    def test_lag_windows_and_precision(self) -> None:
        output = compute_lag(
            self.merged,
            self.bundle.log_records,
            self.config,
            snapshot_version=1,
            package_version="2.0.0",
            logical_clock_floor=1000,
        )
        self.assertEqual(output["lag"]["unreplayed_transaction_count"], 2)
        self.assertEqual(output["lag"]["unreplayed_log_bytes"], 200)
        self.assertEqual(output["committed_watermark"], f"{12:020d}")
        self.assertEqual(output["replay_watermark"], f"{10:020d}")
        self.assertEqual(output["counts"]["migration_decision_count"], 4)
        self.assertEqual(output["counts"]["tombstone_transaction_count"], 1)
        self.assertEqual(len(output["windows"]), 10)
        self.assertEqual(output["windows"][0]["p50_exact"], "5")
        self.assertEqual(output["windows"][0]["p95_exact"], "43/5")
        self.assertTrue(output["precision"]["sensitive_transaction_ids"])
        self.assertTrue(output["precision"]["sensitive_window_indexes"])
        self.assertEqual(output["truth_source"]["kind"], "full_recompute")
        self.assertFalse(output["truth_source"]["missing_time_inference_used"])
        first_comparison = output["precision"]["transaction_comparisons"][0]
        self.assertTrue(first_comparison["error_source_chain"])

    def test_non_integer_scale_keeps_exact_rounding_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            shutil.copy2(self.config.v1_path, temp_path / "positions.jsonl")
            payload = {
                "v1_path": "positions.jsonl",
                "logs_path": str(self.config.logs_path),
                "v1_base_clock_ms": 1000,
                "v1_clock_step_ms": 1,
                "v1_scale": {"numerator": 2, "denominator": 3},
                "v1_rounding": "half_up",
                "window_size": 3,
                "window_stride": 1,
                "tolerance_ms": 3,
                "observation_error_ms": 1,
                "v1_observation_error_ms": 0,
                "require_contiguous": True,
                "strict_clock_order": True,
            }
            (temp_path / "config.json").write_text(stable_json_text(payload), encoding="utf-8")
            config = load_config(temp_path / "config.json")
            from replication_diag.v1_parser import parse_v1

            records = parse_v1(config.v1_path, config)
            first = records[0]
            self.assertEqual(first.delay_ms, 3)
            self.assertEqual(first.migration["rounding_error_ms"], "1/3")
            self.assertEqual(first.error_upper_bound, __import__("fractions").Fraction(1, 3))


class CliDeterminismTests(unittest.TestCase):
    def test_two_fresh_runs_are_byte_identical_and_verified(self) -> None:
        outputs: list[bytes] = []
        with tempfile.TemporaryDirectory() as temp:
            for index in range(2):
                db = Path(temp) / f"state-{index}.sqlite"
                clean_state(db)
                for command in ("load", "merge"):
                    process = run_cli(command, "--db", str(db))
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                lag = run_cli("lag", "--db", str(db))
                self.assertEqual(lag.returncode, 0, lag.stdout + lag.stderr)
                outputs.append(lag.stdout)
                verify = run_cli("verify", "--db", str(db))
                self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
                self.assertTrue(parse_json_output(verify)["ok"])
            self.assertEqual(outputs[0], outputs[1])

            db = Path(temp) / "state-0.sqlite"
            repeat = run_cli("lag", "--db", str(db))
            self.assertEqual(repeat.stdout, outputs[0])

            global_db = Path(temp) / "global.sqlite"
            clean_state(global_db)
            self.assertEqual(run_cli("--db", str(global_db), "load").returncode, 0)
            self.assertEqual(run_cli("--db", str(global_db), "merge").returncode, 0)
            self.assertEqual(run_cli("--db", str(global_db), "lag").returncode, 0)


class ErrorAndInjectionTests(unittest.TestCase):
    def test_all_configured_injections_return_expected_business_code(self) -> None:
        report = run_injection_manifest(CONFIG, INJECTIONS)
        self.assertTrue(report["ok"])
        self.assertEqual(report["count"], 19)
        for result in report["results"]:
            self.assertTrue(result["passed"], result)
            self.assertEqual(result["observed_error"], result["expected_error"])

    def test_invalid_source_does_not_create_state_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            bad_v1 = temp_path / "bad.jsonl"
            bad_v1.write_text(
                '{"tx_id":"bad","relative_delay_ms":1,"payload_bytes":1,"committed":true,'
                '"logical_clock":1000}\n',
                encoding="utf-8",
            )
            payload = {
                "v1_path": "bad.jsonl",
                "logs_path": str(ROOT / "samples" / "logs"),
                "v1_base_clock_ms": 1000,
                "v1_clock_step_ms": 1,
                "v1_scale": "1/1",
                "v1_rounding": "half_up",
                "window_size": 3,
                "window_stride": 1,
                "tolerance_ms": 3,
                "observation_error_ms": 1,
                "v1_observation_error_ms": 0,
                "require_contiguous": True,
                "strict_clock_order": True,
            }
            config_path = temp_path / "config.json"
            config_path.write_text(stable_json_text(payload), encoding="utf-8")
            state = temp_path / "state.sqlite"
            process = run_cli("load", "--config", str(config_path), "--db", str(state))
            self.assertEqual(process.returncode, 1)
            self.assertEqual(parse_json_output(process)["error"]["code"], "E_TX_ID")
            self.assertFalse(state.exists())


class PackageAndRecoveryTests(unittest.TestCase):
    def _baseline(self, db: Path) -> None:
        clean_state(db)
        for command in ("load", "merge"):
            process = run_cli(command, "--db", str(db))
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def test_full_replace_rollback_and_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.sqlite"
            self._baseline(db)

            staged = run_cli("load", "--db", str(db), "--package", str(FULL_PACKAGE))
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            self.assertEqual(parse_json_output(staged)["classification"], "full_replace")
            merged = run_cli("merge", "--db", str(db))
            self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
            lag = parse_json_output(run_cli("lag", "--db", str(db)))
            self.assertEqual(lag["snapshot_version"], 2)
            self.assertEqual(lag["package_version"], "2.2.0")
            self.assertTrue(all(tx["source_version"] == "v2" for tx in lag["transactions"]))

            noop = run_cli("load", "--db", str(db), "--package", str(FULL_PACKAGE))
            self.assertEqual(noop.returncode, 0, noop.stdout + noop.stderr)
            self.assertEqual(parse_json_output(noop)["status"], "noop")

            before = parse_json_output(run_cli("status", "--db", str(db)))
            rollback = run_cli(
                "load", "--db", str(db), "--package", str(POSITION_ROLLBACK_PACKAGE)
            )
            self.assertEqual(rollback.returncode, 1)
            self.assertEqual(
                parse_json_output(rollback)["error"]["code"], "E_POSITION_ROLLBACK"
            )
            version_rollback = run_cli(
                "load", "--db", str(db), "--package", str(VERSION_ROLLBACK_PACKAGE)
            )
            self.assertEqual(version_rollback.returncode, 1)
            self.assertEqual(
                parse_json_output(version_rollback)["error"]["code"],
                "E_VERSION_ROLLBACK",
            )
            after = parse_json_output(run_cli("status", "--db", str(db)))
            self.assertEqual(before["active_snapshot"], after["active_snapshot"])
            self.assertEqual(after["pending_operations"], [])

    def test_incremental_package_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.sqlite"
            self._baseline(db)
            staged = run_cli(
                "load", "--db", str(db), "--package", str(INCREMENTAL_PACKAGE)
            )
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            self.assertEqual(
                parse_json_output(staged)["classification"], "incremental_append"
            )
            status = parse_json_output(run_cli("status", "--db", str(db)))
            self.assertEqual(len(status["pending_operations"]), 1)
            merged = run_cli("merge", "--db", str(db))
            self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
            lag = parse_json_output(run_cli("lag", "--db", str(db)))
            self.assertEqual(lag["counts"]["transaction_count"], 14)
            self.assertEqual(lag["lag"]["unobserved_committed_transaction_count"], 2)
            self.assertEqual(lag["lag"]["unreplayed_transaction_count"], 4)
            self.assertEqual(lag["lag"]["unreplayed_total_bytes"], 410)
            self.assertTrue(parse_json_output(run_cli("verify", "--db", str(db)))["ok"])

    def test_forced_termination_then_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.sqlite"
            clean_state(db)
            load = run_cli("load", "--db", str(db))
            self.assertEqual(load.returncode, 0)
            crashed = run_cli(
                "merge",
                "--db",
                str(db),
                env={"RD_CRASH_BEFORE_COMMIT": "1"},
            )
            self.assertEqual(crashed.returncode, 137)
            status = parse_json_output(run_cli("status", "--db", str(db)))
            self.assertIsNone(status["active_snapshot"])
            self.assertEqual(len(status["pending_operations"]), 1)
            resumed = run_cli("resume", "--db", str(db))
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            self.assertEqual(parse_json_output(resumed)["status"], "resumed")
            self.assertTrue(parse_json_output(run_cli("verify", "--db", str(db)))["ok"])
            again = run_cli("resume", "--db", str(db))
            self.assertEqual(parse_json_output(again)["status"], "noop")


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_load_and_merge_commit_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.sqlite"
            clean_state(db)
            env = os.environ.copy()
            load_processes = [
                subprocess.Popen(
                    [sys.executable, "-m", "replication_diag", "load", "--db", str(db)],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]
            for process in load_processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stdout + stderr)

            merge_processes = [
                subprocess.Popen(
                    [sys.executable, "-m", "replication_diag", "merge", "--db", str(db)],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]
            for process in merge_processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertTrue(json.loads(stdout)["ok"])

            connection = sqlite3.connect(db)
            try:
                snapshots = connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                source_ops = connection.execute(
                    "SELECT COUNT(*) FROM operations WHERE kind='source' AND state='committed'"
                ).fetchone()[0]
                active_count = connection.execute(
                    "SELECT COUNT(*) FROM active_snapshot WHERE snapshot_id IS NOT NULL"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(snapshots, 1)
            self.assertEqual(source_ops, 1)
            self.assertEqual(active_count, 1)
            self.assertTrue(parse_json_output(run_cli("verify", "--db", str(db)))["ok"])


if __name__ == "__main__":
    unittest.main()
