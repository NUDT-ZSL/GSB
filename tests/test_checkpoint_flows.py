import json
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ckptmgr.errors import ShardWriteFailedError
from ckptmgr.jsonio import digest_json
from ckptmgr.manager import CheckpointManager
from ckptmgr.model import load_plan
from tests.helpers import (
    cli_env,
    plan_args,
    run_cli,
    sample_paths,
    save_cmd,
)


def read_db(root: Path):
    connection = sqlite3.connect(str(root / ".ckpt" / "ledger.sqlite3"))
    connection.row_factory = sqlite3.Row
    return connection


class CheckpointFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.paths = sample_paths(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, root: Path, version: int, clock: int, mode: str = "auto", check=True):
        return run_cli(
            *save_cmd(root, self.paths, version, clock, mode),
            check=check,
        )

    def restore(self, root: Path, version: int):
        return run_cli(
            "restore",
            "--root",
            str(root),
            "--checkpoint",
            "demo",
            "--version",
            str(version),
            check=True,
        )[1]

    def test_full_save_restore_verify_and_repeat_idempotency(self):
        root = self.base / "root"
        completed, saved = self.save(root, 1, 1000, "full")
        self.assertEqual(completed.returncode, 0)
        self.assertFalse(saved["idempotent"])
        restored = self.restore(root, 1)
        self.assertTrue(restored["stitched_equals_independent"])
        self.assertEqual(
            restored["parameter_digest"], saved["parameter_digest"]
        )
        completed, repeated = self.save(root, 1, 1050, "full")
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(repeated["idempotent"])
        with read_db(root) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0], 4
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM parameter_digests"
                ).fetchone()[0],
                9,
            )
        verified = run_cli("verify", "--root", str(root), check=True)[1]
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["verified_checkpoints"], 1)
        validated_manifest = run_cli(
            "validate",
            "--manifest",
            str(root / "manifests" / "demo" / "1.json"),
            check=True,
        )[1]["manifest"]
        self.assertEqual(validated_manifest["layer_count"], 9)
        self.assertEqual(validated_manifest["shard_count"], 4)
        materialized = self.base / "materialized"
        restored_with_files = run_cli(
            "restore",
            "--root",
            str(root),
            "--checkpoint",
            "demo",
            "--version",
            "1",
            "--materialize",
            str(materialized),
            check=True,
        )[1]
        self.assertEqual(restored_with_files["materialized_layers"], 9)
        index = json.loads((materialized / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index["layers"]), 9)
        self.assertEqual(
            sum((materialized / item["file"]).stat().st_size for item in index["layers"]),
            sum(item["nbytes"] for item in index["layers"]),
        )

    def test_same_plan_is_bytewise_deterministic_across_workspaces(self):
        root_a = self.base / "a"
        root_b = self.base / "b"
        self.save(root_a, 1, 1000, "full")
        self.save(root_b, 1, 1000, "full")
        files_a = sorted(
            path.relative_to(root_a / "objects")
            for path in (root_a / "objects").rglob("*.shard")
        )
        files_b = sorted(
            path.relative_to(root_b / "objects")
            for path in (root_b / "objects").rglob("*.shard")
        )
        self.assertEqual(files_a, files_b)
        for relative in files_a:
            self.assertEqual(
                (root_a / "objects" / relative).read_bytes(),
                (root_b / "objects" / relative).read_bytes(),
            )
        self.assertEqual(
            self.restore(root_a, 1)["stitched_digest"],
            self.restore(root_b, 1)["stitched_digest"],
        )

    def test_incremental_and_full_save_have_equivalent_shards(self):
        incremental_root = self.base / "incremental"
        full_root = self.base / "full"
        self.save(incremental_root, 1, 1000, "full")
        self.save(incremental_root, 2, 1100, "incremental")
        self.save(full_root, 1, 1000, "full")
        completed, full_v2 = self.save(full_root, 2, 1100, "full")
        self.assertEqual(completed.returncode, 0)

        incremental_restore = self.restore(incremental_root, 2)
        full_restore = self.restore(full_root, 2)
        self.assertEqual(incremental_restore, full_restore)
        with read_db(incremental_root) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT shard_id, status, reuse_hit, reuse_basis "
                    "FROM round_shards WHERE round_id = 2 ORDER BY shard_index"
                ).fetchall()
            ]
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0], 6
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM checkpoint_shards"
                ).fetchone()[0],
                8,
            )
        self.assertEqual([row["status"] for row in rows].count("reused"), 2)
        self.assertEqual([row["status"] for row in rows].count("committed"), 2)
        reused = [row for row in rows if row["status"] == "reused"]
        self.assertTrue(all(row["reuse_hit"] == 1 for row in reused))
        self.assertTrue(all(row["reuse_basis"] for row in reused))

    def test_clock_regression_has_distinct_business_code(self):
        root = self.base / "clock"
        self.save(root, 1, 1000, "full")
        completed, payload = self.save(root, 2, 100, "full", check=False)
        self.assertEqual(completed.returncode, 10)
        self.assertEqual(payload["error"]["code"], "E_CLOCK_REGRESSION")
        self.assertEqual(payload["error"]["details"]["watermark_ms"], 1015)

    def test_version_rollback_has_distinct_business_code(self):
        root = self.base / "rollback"
        self.save(root, 4, 1000, "full")
        completed, payload = self.save(root, 3, 1100, "full", check=False)
        self.assertEqual(completed.returncode, 9)
        self.assertEqual(payload["error"]["code"], "E_VERSION_ROLLBACK")
        self.assertEqual(payload["error"]["details"]["latest_version"], 4)

    def test_budget_abandonment_and_resume(self):
        root = self.base / "budget"
        strategy = json.loads(Path(self.paths["strategy"]).read_text(encoding="utf-8"))
        strategy["round_budget_ms"] = 6
        strategy["max_concurrency"] = 1
        strategy_path = self.base / "tight-strategy.json"
        strategy_path.write_text(json.dumps(strategy), encoding="utf-8")
        arguments = [
            "save",
            "--root",
            str(root),
            *plan_args(self.paths),
            "--version",
            "1",
            "--clock-ms",
            "1000",
            "--strategy",
            str(strategy_path),
        ]
        completed, payload = run_cli(*arguments)
        self.assertEqual(completed.returncode, 14)
        self.assertEqual(payload["error"]["code"], "E_ROUND_BUDGET_EXHAUSTED")
        status = run_cli("status", "--root", str(root), check=True)[1]
        statuses = [row["status"] for row in status["round_shards"]]
        self.assertEqual(statuses.count("committed"), 1)
        self.assertEqual(statuses.count("budget_unstarted"), 3)

        for index, clock in enumerate((2000, 3000, 4000), start=1):
            completed, payload = run_cli(
                "resume",
                "--root",
                str(root),
                *plan_args(self.paths),
                "--strategy",
                str(strategy_path),
                "--version",
                "1",
                "--clock-ms",
                str(clock),
            )
            if index < 3:
                self.assertEqual(completed.returncode, 14, payload)
                self.assertEqual(
                    payload["error"]["code"], "E_ROUND_BUDGET_EXHAUSTED"
                )
            else:
                self.assertEqual(completed.returncode, 0, payload)
        verified = run_cli("verify", "--root", str(root), check=True)[1]
        self.assertTrue(verified["ok"])

    def test_sigkill_during_write_then_resume(self):
        clean_root = self.base / "clean"
        crash_root = self.base / "crash"
        self.save(clean_root, 1, 1000, "full")
        clean_restore = self.restore(clean_root, 1)
        completed, _ = run_cli(
            *save_cmd(crash_root, self.paths, 1, 1000, "full"),
            "--crash-manifest",
            self.paths["crash_write"],
        )
        self.assertEqual(completed.returncode, -signal.SIGKILL)
        status = run_cli("status", "--root", str(crash_root), check=True)[1]
        self.assertEqual(status["rounds"][0]["status"], "writing")
        self.assertGreaterEqual(status["objects"]["count"], 2)
        self.assertEqual(status["orphan_objects"], [])
        completed, resumed = run_cli(
            "resume",
            "--root",
            str(crash_root),
            *plan_args(self.paths),
            "--version",
            "1",
            "--clock-ms",
            "2000",
            check=True,
        )
        self.assertFalse(resumed["idempotent"])
        self.assertEqual(self.restore(crash_root, 1), clean_restore)
        status = run_cli("status", "--root", str(crash_root), check=True)[1]
        self.assertEqual(status["orphan_objects"], [])
        self.assertEqual(status["objects"]["count"], 4)
        self.assertEqual(status["rounds"][0]["status"], "complete")
        run_cli("verify", "--root", str(crash_root), check=True)

    def test_sigkill_during_commit_then_resume(self):
        root = self.base / "commit-crash"
        completed, _ = run_cli(
            *save_cmd(root, self.paths, 1, 1000, "full"),
            "--crash-manifest",
            self.paths["crash_commit"],
        )
        self.assertEqual(completed.returncode, -signal.SIGKILL)
        with read_db(root) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM pointers").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT status FROM rounds").fetchone()[0],
                "committing",
            )
        run_cli(
            "resume",
            "--root",
            str(root),
            "--checkpoint",
            "demo",
            "--version",
            "1",
            "--clock-ms",
            "2000",
            check=True,
        )
        with read_db(root) as connection:
            self.assertEqual(
                connection.execute("SELECT version FROM pointers").fetchone()[0], 1
            )
        run_cli("verify", "--root", str(root), check=True)

    def test_multiple_save_processes_commit_once(self):
        root = self.base / "concurrent"
        processes = []
        for _ in range(4):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "ckptmgr",
                        *save_cmd(root, self.paths, 1, 1000, "full"),
                    ],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    env=cli_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        results = []
        for process in processes:
            stdout, stderr = process.communicate()
            self.assertEqual(process.returncode, 0, (stdout, stderr))
            results.append(json.loads(stdout))
        self.assertEqual(sum(1 for item in results if not item["idempotent"]), 1)
        self.assertEqual(sum(1 for item in results if item["idempotent"]), 3)
        with read_db(root) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM checkpoint_shards").fetchone()[0],
                4,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0], 4
            )
        run_cli("verify", "--root", str(root), check=True)

    def test_failed_wave_actively_abandons_later_waves_then_resume_recovers(self):
        root = self.base / "failed-wave"
        plan = load_plan(
            self.paths["plan"], self.paths["strategy"], self.paths["save_plan"]
        )
        manager = CheckpointManager(root)
        original = CheckpointManager._write_one_shard

        def flaky(instance, round_id, shard, *args, **kwargs):
            if shard.shard_index == 0:
                raise OSError("injected started-shard failure")
            return original(instance, round_id, shard, *args, **kwargs)

        with mock.patch.object(CheckpointManager, "_write_one_shard", flaky):
            with self.assertRaises(ShardWriteFailedError):
                manager.save(plan, version=1, mode="full", clock_ms=1000)
        status = manager.status()
        shard_status = {
            row["shard_id"]: row["status"] for row in status["round_shards"]
        }
        self.assertIn("started_failed", shard_status.values())
        self.assertIn("abandoned", shard_status.values())
        manager.resume(plan, version=1, clock_ms=2000)
        self.assertEqual(self.restore(root, 1)["parameter_digest"], manager.verify()["checkpoints"][0]["parameter_digest"])

    def test_restore_error_codes_are_distinct_and_do_not_write_partial_output(self):
        root = self.base / "errors"
        self.save(root, 1, 1000, "full")

        missing_root = self.base / "missing"
        shutil.copytree(root, missing_root)
        victim = sorted((missing_root / "objects").rglob("*.shard"))[0]
        victim.unlink()
        output = self.base / "must-not-exist.json"
        materialized = self.base / "must-not-exist-dir"
        completed, payload = run_cli(
            "restore",
            "--root",
            str(missing_root),
            "--checkpoint",
            "demo",
            "--version",
            "1",
            "--output",
            str(output),
            "--materialize",
            str(materialized),
        )
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(payload["error"]["code"], "E_SHARD_MISSING")
        self.assertFalse(output.exists())
        self.assertFalse(materialized.exists())

        digest_root = self.base / "digest"
        shutil.copytree(root, digest_root)
        victim = sorted((digest_root / "objects").rglob("*.shard"))[0]
        data = bytearray(victim.read_bytes())
        data[-1] ^= 0xFF
        victim.write_bytes(bytes(data))
        completed, payload = run_cli(
            "restore",
            "--root",
            str(digest_root),
            "--checkpoint",
            "demo",
            "--version",
            "1",
        )
        self.assertEqual(completed.returncode, 4)
        self.assertEqual(payload["error"]["code"], "E_SHARD_DIGEST_MISMATCH")

        order_root = self.base / "order"
        shutil.copytree(root, order_root)
        with read_db(order_root) as connection:
            row = connection.execute(
                "SELECT manifest_json FROM checkpoints"
            ).fetchone()
            manifest = json.loads(row["manifest_json"])
            manifest["layer_order"][0], manifest["layer_order"][1] = (
                manifest["layer_order"][1],
                manifest["layer_order"][0],
            )
            manifest_json = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            connection.execute(
                "UPDATE checkpoints SET manifest_json = ?, manifest_digest = ?",
                (manifest_json, digest_json(manifest)),
            )
            connection.commit()
        (order_root / "manifests" / "demo" / "1.json").write_text(
            manifest_json + "\n", encoding="utf-8"
        )
        completed, payload = run_cli(
            "restore",
            "--root",
            str(order_root),
            "--checkpoint",
            "demo",
            "--version",
            "1",
        )
        self.assertEqual(completed.returncode, 5)
        self.assertEqual(payload["error"]["code"], "E_STITCH_ORDER_MISMATCH")

        param_root = self.base / "param"
        shutil.copytree(root, param_root)
        with read_db(param_root) as connection:
            row = connection.execute(
                "SELECT manifest_json FROM checkpoints"
            ).fetchone()
            manifest = json.loads(row["manifest_json"])
            manifest["parameter_digest"] = "0" * 64
            manifest_json = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            connection.execute(
                "UPDATE checkpoints SET manifest_json = ?, manifest_digest = ?",
                (manifest_json, digest_json(manifest)),
            )
            connection.commit()
        (param_root / "manifests" / "demo" / "1.json").write_text(
            manifest_json + "\n", encoding="utf-8"
        )
        completed, payload = run_cli(
            "restore",
            "--root",
            str(param_root),
            "--checkpoint",
            "demo",
            "--version",
            "1",
        )
        self.assertEqual(completed.returncode, 6)
        self.assertEqual(payload["error"]["code"], "E_PARAMETER_DIGEST_MISMATCH")

    def test_verify_reports_corruption(self):
        root = self.base / "verify"
        self.save(root, 1, 1000, "full")
        victim = sorted((root / "objects").rglob("*.shard"))[0]
        data = bytearray(victim.read_bytes())
        data[-1] ^= 0x55
        victim.write_bytes(bytes(data))
        completed, payload = run_cli("verify", "--root", str(root))
        self.assertEqual(completed.returncode, 17)
        self.assertEqual(payload["error"]["code"], "E_VERIFY_FAILED")
        codes = {
            failure["error"]["code"] for failure in payload["error"]["details"]["failures"]
        }
        self.assertIn("E_SHARD_DIGEST_MISMATCH", codes)

    def test_multiple_restore_processes_and_all_live_versions(self):
        root = self.base / "multi-restore"
        self.save(root, 1, 1000, "full")
        self.save(root, 2, 1100, "incremental")
        self.save(root, 3, 1200, "incremental")
        processes = []
        for _ in range(4):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "ckptmgr",
                        "restore",
                        "--root",
                        str(root),
                        "--checkpoint",
                        "demo",
                        "--version",
                        "2",
                    ],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    env=cli_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        payloads = []
        for process in processes:
            stdout, stderr = process.communicate()
            self.assertEqual(process.returncode, 0, (stdout, stderr))
            payloads.append(json.loads(stdout))
        self.assertTrue(all(item == payloads[0] for item in payloads))
        verified = run_cli("verify", "--root", str(root), check=True)[1]
        self.assertEqual(verified["verified_checkpoints"], 3)
        self.assertEqual(
            [item["version"] for item in verified["checkpoints"]], [1, 2, 3]
        )


if __name__ == "__main__":
    unittest.main()
