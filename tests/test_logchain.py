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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from logchain.cli import run as cli_run
from logchain.engine import cmd_merge, cmd_replay
from logchain.errors import ChainError
from logchain.packages import receive_package
from logchain.seed import generate
from logchain.util import read_json, stable_json, write_stable_json


class LogChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="logchain-test-"))
        self.root = self.temp / "work"
        self.root.mkdir()
        generate(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def cli(self, *args: str) -> tuple[int, dict]:
        exit_code, output = cli_run([*args, "--root", str(self.root)])
        return exit_code, json.loads(output)

    def test_seed_generation_is_byte_deterministic(self) -> None:
        other = self.temp / "second"
        other.mkdir()
        generate(other)
        files = sorted(
            path.relative_to(self.root)
            for path in self.root.rglob("*")
            if path.is_file() and ".venv" not in path.parts
        )
        for relative in files:
            self.assertEqual(
                (self.root / relative).read_bytes(),
                (other / relative).read_bytes(),
                f"seed file differs: {relative}",
            )

    def test_validation_error_codes_are_distinct(self) -> None:
        v1_path = self.root / "samples" / "snapshots" / "v1_snapshot.json"
        v1 = read_json(v1_path)
        v1["entries"][1]["row_key"] = v1["entries"][0]["row_key"]
        write_stable_json(v1_path, v1)
        with self.assertRaises(ChainError) as context:
            cmd_merge(self.root)
        self.assertEqual(context.exception.code, "V1_DUPLICATE_ROW_KEY")
        self.assertEqual(context.exception.details["table_name"], "accounts")
        self.assertTrue(context.exception.details["row_key"])

        generate(self.root)
        shard_ref = read_json(self.root / "samples" / "v2" / "manifest.json")["shards"][0]
        shard_path = self.root / "samples" / "v2" / shard_ref["path"]
        shard = read_json(shard_path)
        shard["entries"][0]["commit_ms"] += 1
        write_stable_json(shard_path, shard)
        with self.assertRaises(ChainError) as v2_context:
            cmd_merge(self.root)
        self.assertEqual(v2_context.exception.code, "V2_SHARD_HASH_MISMATCH")
        self.assertIn("actual_sha256", v2_context.exception.details)

    def test_merge_replay_status_and_verify(self) -> None:
        exit_code, load_result = self.cli("load")
        self.assertEqual(exit_code, 0)
        self.assertEqual(load_result["wal_segments"], 2)

        exit_code, merge_result = self.cli("merge")
        self.assertEqual(exit_code, 0)
        self.assertEqual(merge_result["decision"], "view_switched")

        exit_code, replay_result = self.cli("replay")
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(replay_result["applications"]), 3)

        exit_code, verify_result = self.cli("verify")
        self.assertEqual(exit_code, 0, verify_result)
        self.assertTrue(verify_result["full_replay_equivalence"])

        _, status_one = self.cli("status")
        _, status_two = self.cli("status")
        self.assertEqual(stable_json(status_one), stable_json(status_two))
        self.assertEqual(status_one["table_counts"]["accounts"]["row_count"], 6)
        self.assertEqual(status_one["table_counts"]["accounts"]["tombstone_count"], 1)
        self.assertTrue(status_one["precision_sensitive"])
        flag = status_one["precision_sensitive"][0]
        self.assertEqual(flag["commit_gap_ms_text"], "1/1")
        self.assertLess(
            flag["comparison_error_upper_bound_ms"]["numerator"]
            / flag["comparison_error_upper_bound_ms"]["denominator"],
            1,
        )
        self.assertEqual(
            status_one["generation_boundary"]["decision"],
            "advance_atomically_with_pinned_canonical_order",
        )

    def test_repeated_merge_artifact_is_bitwise_identical(self) -> None:
        cmd_merge(self.root)
        artifact = self.root / "artifacts" / "logchain-merge.json"
        first = artifact.read_bytes()
        cmd_merge(self.root)
        second = artifact.read_bytes()
        self.assertEqual(first, second)

    def test_snapshot_package_noop_and_rollback(self) -> None:
        package_dir = self.root / "samples" / "v2"
        first = receive_package(self.root, package_dir)
        self.assertEqual(first["decision"], "full_replace")
        second = receive_package(self.root, package_dir)
        self.assertEqual(second["decision"], "noop")

        rollback_dir = self.temp / "rollback-package"
        shutil.copytree(package_dir, rollback_dir)
        manifest = read_json(rollback_dir / "manifest.json")
        manifest["manifest_version"] = 0
        write_stable_json(rollback_dir / "manifest.json", manifest)
        with self.assertRaises(ChainError) as context:
            receive_package(self.root, rollback_dir)
        self.assertEqual(context.exception.code, "SNAPSHOT_VERSION_ROLLBACK")

    def subprocess_cli(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
        return subprocess.run(
            [sys.executable, "-m", "logchain", *args, "--root", str(root)],
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_replay_crash_recovery_matches_normal(self) -> None:
        normal = self.temp / "normal"
        crash = self.temp / "crash"
        shutil.copytree(self.root, normal)
        shutil.copytree(self.root, crash)

        for directory in (normal, crash):
            self.subprocess_cli(directory, "merge")
        normal_replay = self.subprocess_cli(normal, "replay")
        self.assertEqual(normal_replay.returncode, 0, normal_replay.stderr)

        crashed = self.subprocess_cli(
            crash, "replay", "--crash-at", "crash-replay-g1-mid"
        )
        self.assertNotEqual(crashed.returncode, 0)
        crash_db = sqlite3.connect(crash / "runtime" / "logchain.sqlite")
        status = dict(
            crash_db.execute(
                "SELECT segment_id, status FROM applied_segments WHERE segment_id='wal-g1-0001'"
            ).fetchall()
        )
        self.assertEqual(status["wal-g1-0001"], "applying")
        crash_db.close()

        resume = self.subprocess_cli(crash, "resume")
        self.assertEqual(resume.returncode, 0, resume.stderr)
        verified = self.subprocess_cli(crash, "verify")
        self.assertEqual(verified.returncode, 0, verified.stderr)

        normal_status = json.loads(self.subprocess_cli(normal, "status").stdout)
        crash_status = json.loads(self.subprocess_cli(crash, "status").stdout)
        self.assertEqual(normal_status["ledger_digest"], crash_status["ledger_digest"])
        self.assertEqual(
            normal_status["table_counts"], crash_status["table_counts"]
        )
        self.assertEqual(
            crash_status["recovery"]["archive_conclusion"]["status"],
            "RECOVERED_CONSISTENT",
        )

    def test_concurrent_replay_is_idempotent(self) -> None:
        self.subprocess_cli(self.root, "merge")
        processes = [
            subprocess.Popen(
                [sys.executable, "-m", "logchain", "replay", "--root", str(self.root)],
                cwd=PROJECT_ROOT,
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        results = [process.wait() for process in processes]
        errors = [process.stderr.read() for process in processes]
        for process in processes:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        self.assertEqual(results, [0, 0, 0, 0], errors)
        exit_code, verify = self.cli("verify")
        self.assertEqual(exit_code, 0, verify)
        db = sqlite3.connect(self.root / "runtime" / "logchain.sqlite")
        applied = db.execute(
            "SELECT COUNT(*) FROM applied_segments WHERE status='applied'"
        ).fetchone()[0]
        duplicate_segments = db.execute(
            "SELECT COUNT(*) FROM (SELECT segment_id FROM applied_segments GROUP BY segment_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        db.close()
        self.assertEqual(applied, 3)
        self.assertEqual(duplicate_segments, 0)

    def test_concurrent_package_load_switches_pointer_once(self) -> None:
        package_dir = self.root / "samples" / "v2"
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "logchain",
                    "load",
                    "--package",
                    str(package_dir),
                    "--root",
                    str(self.root),
                ],
                cwd=PROJECT_ROOT,
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout)["decision"])
        self.assertEqual(outputs.count("full_replace"), 1, outputs)
        self.assertEqual(outputs.count("noop"), 3, outputs)

    def test_merge_and_checkpoint_crash_recovery(self) -> None:
        normal = self.temp / "normal-all-crashes"
        shutil.copytree(self.root, normal)
        self.assertEqual(self.subprocess_cli(normal, "merge").returncode, 0)
        self.assertEqual(self.subprocess_cli(normal, "replay").returncode, 0)
        normal_status = json.loads(self.subprocess_cli(normal, "status").stdout)

        scenarios = [
            ("crash-merge-staging", [], "merge"),
            (
                "crash-checkpoint-g2-before-commit",
                ["merge"],
                "replay",
            ),
        ]
        for crash_id, setup_commands, crash_command in scenarios:
            with self.subTest(crash_id=crash_id):
                crash_root = self.temp / crash_id
                shutil.copytree(self.root, crash_root)
                for setup_command in setup_commands:
                    self.assertEqual(
                        self.subprocess_cli(crash_root, setup_command).returncode, 0
                    )
                crashed = self.subprocess_cli(
                    crash_root, crash_command, "--crash-at", crash_id
                )
                self.assertNotEqual(crashed.returncode, 0)
                resumed = self.subprocess_cli(crash_root, "resume")
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                verified = self.subprocess_cli(crash_root, "verify")
                self.assertEqual(verified.returncode, 0, verified.stderr)
                recovered_status = json.loads(
                    self.subprocess_cli(crash_root, "status").stdout
                )
                self.assertEqual(
                    normal_status["ledger_digest"],
                    recovered_status["ledger_digest"],
                )
                self.assertEqual(
                    normal_status["table_counts"],
                    recovered_status["table_counts"],
                )
                self.assertEqual(
                    recovered_status["recovery"]["archive_conclusion"]["status"],
                    "RECOVERED_CONSISTENT",
                )


if __name__ == "__main__":
    unittest.main()
