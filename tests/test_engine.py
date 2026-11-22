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

import yaml

from mcde.engine import Engine
from mcde.errors import (
    E_DUP_APP,
    E_LIMIT,
    E_OVERRIDE_CONFLICT,
    E_QUOTA_REPLICAS,
    E_REF_CLUSTER,
    E_REF_REGION,
    E_TYPE,
    E_VERSION_ROLLBACK,
    EngineError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "samples"


class EngineTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = Path(tempfile.mkdtemp(prefix="mcde-test-"))
        self.samples = self.tempdir / "samples"
        shutil.copytree(SAMPLES, self.samples)
        self.db = self.tempdir / "state" / "mcde.sqlite3"

    def tearDown(self) -> None:
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def engine(self, samples: Path | None = None) -> Engine:
        return Engine(
            samples_dir=samples or self.samples,
            db_path=self.db,
        )

    def sample_path(self, name: str) -> Path:
        return SAMPLES / "negative" / name

    def run_cli(
        self,
        command: str,
        *,
        db: Path | None = None,
        samples: Path | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            "-m",
            "mcde",
            "--db",
            str(db or self.db),
            "--samples",
            str(samples or self.samples),
            command,
        ]
        cmd.extend(args or [])
        child_env = os.environ.copy()
        if env:
            child_env.update(env)
        return subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=child_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def query_one(self, sql: str, parameters: tuple[object, ...] = ()) -> object:
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(sql, parameters).fetchone()
            return row[0] if row else None
        finally:
            conn.close()


class ValidationTests(EngineTestBase):
    def test_reference_type_and_limit_codes_do_not_create_state(self) -> None:
        cases = [
            ("ref_region", E_REF_REGION),
            ("ref_cluster", E_REF_CLUSTER),
            ("type_error", E_TYPE),
            ("bad_limit", E_LIMIT),
        ]
        for folder, expected in cases:
            with self.subTest(folder=folder):
                db = self.tempdir / f"{folder}.sqlite3"
                engine = Engine(
                    samples_dir=self.sample_path(folder), db_path=db
                )
                with self.assertRaises(EngineError) as raised:
                    engine.load()
                self.assertEqual(raised.exception.code, expected)
                self.assertFalse(db.exists())

    def test_duplicate_application_has_distinct_code(self) -> None:
        global_path = self.samples / "global.yaml"
        global_doc = yaml.safe_load(global_path.read_text(encoding="utf-8"))
        global_doc["apps"].append(dict(global_doc["apps"][0]))
        global_path.write_text(
            yaml.safe_dump(global_doc, sort_keys=False),
            encoding="utf-8",
        )
        with self.assertRaises(EngineError) as raised:
            self.engine().load()
        self.assertEqual(raised.exception.code, E_DUP_APP)
        self.assertFalse(self.db.exists())

    def test_invalid_reload_does_not_change_existing_state(self) -> None:
        engine = self.engine()
        load = engine.load()
        digest_before = load["snapshot_digest"]
        snapshot_count_before = self.query_one(
            "SELECT COUNT(*) FROM snapshots"
        )
        rules_path = self.samples / "rules.yaml"
        rules_path.write_text(
            "approval:\n"
            "  require_for_diff: true\n"
            "  ttl_ms: 10000\n"
            "change_window:\n"
            "  start_ms: 1000000\n"
            "  end_ms: 1100000\n"
            "  period_ms: 1000000\n"
            "max_parallel: invalid\n"
            "round_budget_ms: 100\n",
            encoding="utf-8",
        )
        with self.assertRaises(EngineError) as raised:
            engine.load()
        self.assertEqual(raised.exception.code, E_TYPE)
        self.assertEqual(
            self.query_one(
                "SELECT digest FROM active_snapshot WHERE id=1"
            ),
            digest_before,
        )
        self.assertEqual(
            self.query_one("SELECT COUNT(*) FROM snapshots"),
            snapshot_count_before,
        )


class MergeAndPolicyTests(EngineTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.engine().load()

    def test_provenance_conflicts_approvals_and_quota(self) -> None:
        merge = self.engine().merge()
        self.assertEqual(merge["summary"]["accepted"], 4)
        self.assertEqual(merge["summary"]["rejected"], 4)

        east_a = merge["clusters"]["east-a"]
        self.assertEqual(east_a["status"], "accepted")
        replicas = next(
            key
            for key in east_a["keys"]
            if key["key"] == "apps.checkout.replicas"
        )
        self.assertEqual(replicas["value"], 5)
        self.assertEqual(replicas["source_layer"], "cluster")
        self.assertEqual(
            replicas["overridden_layers"], ["global", "region"]
        )
        self.assertEqual(
            [step["layer"] for step in replicas["merge_path"]],
            ["global", "region", "cluster"],
        )
        self.assertEqual(
            replicas["policy"]["approval"]["approval_id"],
            "APR-EAST-A-REPLICAS",
        )

        west_a = merge["clusters"]["west-a"]
        self.assertEqual(west_a["rejection_code"], E_OVERRIDE_CONFLICT)
        conflict = west_a["first_violation"]
        self.assertEqual(conflict["key"], "apps.checkout.replicas")
        self.assertEqual(conflict["source_layer"], "cluster")
        self.assertEqual(conflict["region_value"], 2)
        self.assertEqual(conflict["cluster_value"], 3)

        self.assertEqual(
            merge["clusters"]["east-b"]["rejection_code"],
            "E_APPROVAL_EXPIRED",
        )
        self.assertEqual(
            merge["clusters"]["east-c"]["rejection_code"],
            "E_APPROVAL_SCOPE_MISMATCH",
        )

        north = merge["clusters"]["north-a"]
        self.assertEqual(north["rejection_code"], E_QUOTA_REPLICAS)
        self.assertEqual(
            north["first_violation"]["key"], "apps.payments.replicas"
        )
        self.assertLess(north["first_violation"]["remaining_after"], 0)

    def test_negative_unauthorized_and_quota_samples(self) -> None:
        unauthorized = Engine(
            samples_dir=self.sample_path("unauthorized"),
            db_path=self.tempdir / "unauthorized.sqlite3",
        )
        unauthorized.load()
        plan = unauthorized.merge()
        self.assertEqual(
            plan["clusters"]["east-a"]["rejection_code"],
            E_OVERRIDE_CONFLICT,
        )

        quota = Engine(
            samples_dir=self.sample_path("quota"),
            db_path=self.tempdir / "quota.sqlite3",
        )
        quota.load()
        plan = quota.merge()
        self.assertEqual(
            plan["clusters"]["east-a"]["rejection_code"],
            E_QUOTA_REPLICAS,
        )


class ApplyTests(EngineTestBase):
    def test_apply_status_verify_and_idempotency(self) -> None:
        engine = self.engine()
        engine.load()
        first = engine.apply()
        statuses = {
            item["cluster"]: item["dispatch_status"]
            for item in first["clusters"]
        }
        self.assertEqual(
            statuses,
            {
                "east-a": "applied",
                "east-b": "rejected",
                "east-c": "rejected",
                "north-a": "rejected",
                "south-a": "applied",
                "south-b": "applied",
                "west-a": "rejected",
                "west-b": "failed_after_start",
            },
        )
        self.assertEqual(first["version"], 1)

        status = engine.status()
        east_a = next(
            item
            for item in status["clusters"]
            if item["cluster"] == "east-a"
        )
        self.assertEqual(east_a["current_version"], 1)
        self.assertEqual(east_a["replicas"]["used"], 9)
        self.assertEqual(east_a["replicas"]["remaining"], 1)
        self.assertEqual(east_a["resources"]["used"], 330)
        self.assertEqual(east_a["conflict_count"], 0)
        self.assertEqual(status["summary"]["conflicts"], 5)

        verification = engine.verify()
        self.assertTrue(verification["ok"])
        self.assertEqual(verification["unexplained_drift_count"], 0)
        self.assertGreater(verification["explained_residual_count"], 0)
        for residual in verification["residuals"]:
            self.assertTrue(residual["basis"])
            self.assertEqual(residual["verdict"], "explained_residual")

        second = engine.apply()
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["version"], 1)
        self.assertEqual(
            self.query_one("SELECT COUNT(*) FROM config_versions"), 1
        )
        self.assertEqual(
            self.query_one(
                "SELECT COUNT(*) FROM approval_uses "
                "WHERE approval_id = 'APR-EAST-A-REPLICAS'"
            ),
            1,
        )
        self.assertEqual(
            self.query_one("SELECT replicas_used FROM ledger WHERE cluster_id='east-a'"),
            9,
        )
        self.assertEqual(
            self.query_one(
                "SELECT COUNT(*) FROM ledger "
                "WHERE replicas_used < 0 OR resources_used < 0 "
                "OR replicas_used > replicas_capacity "
                "OR resources_used > resources_capacity"
            ),
            0,
        )

        status_text_one = json.dumps(
            engine.status(), sort_keys=True, ensure_ascii=False
        )
        status_text_two = json.dumps(
            engine.status(), sort_keys=True, ensure_ascii=False
        )
        self.assertEqual(status_text_one, status_text_two)

    def test_verify_detects_unexplained_drift(self) -> None:
        engine = self.engine()
        engine.load()
        engine.apply()
        readback = engine.repo.cluster_readback("east-a")
        self.assertIsNotNone(readback)
        config = readback["config"]
        config["apps"]["checkout"]["replicas"] = 8
        engine.repo.debug_set_readback("east-a", config)
        result = engine.verify()
        self.assertFalse(result["ok"])
        self.assertGreater(result["unexplained_drift_count"], 0)
        drift = next(
            item
            for item in result["residuals"]
            if item["verdict"] == "unexplained_drift"
        )
        self.assertEqual(drift["key"], "apps.checkout.replicas")

    def test_window_queue_and_resume(self) -> None:
        rules_path = self.samples / "rules.yaml"
        rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
        rules["change_window"]["start_ms"] = 1000100
        rules_path.write_text(
            yaml.safe_dump(rules, sort_keys=False), encoding="utf-8"
        )
        engine = self.engine()
        engine.load()
        queued = engine.apply()
        self.assertEqual(queued["logical_clock_ms"], 1000000)
        queue_statuses = {
            item["cluster"]: item["dispatch_status"]
            for item in queued["clusters"]
        }
        self.assertEqual(queue_statuses["east-a"], "queued")
        self.assertEqual(queue_statuses["south-a"], "queued")
        self.assertEqual(queued["rounds"][0]["earliest_ms"], 1000100)
        resumed = engine.resume()
        self.assertEqual(resumed["logical_clock_ms"], 1000220)
        resumed_statuses = {
            item["cluster"]: item["dispatch_status"]
            for item in resumed["clusters"]
        }
        self.assertEqual(resumed_statuses["east-a"], "applied")
        self.assertEqual(resumed_statuses["west-b"], "failed_after_start")
        self.assertIn(
            "window_reopened",
            [round_info["status"] for round_info in resumed["rounds"]],
        )

    def test_budget_exhaustion_and_operator_abandon(self) -> None:
        rules_path = self.samples / "rules.yaml"
        rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
        rules["round_budget_ms"] = 20
        rules_path.write_text(
            yaml.safe_dump(rules, sort_keys=False), encoding="utf-8"
        )
        engine = self.engine()
        engine.load()
        result = engine.apply()
        statuses = {
            item["cluster"]: item["dispatch_status"]
            for item in result["clusters"]
        }
        for cluster in ("east-a", "south-a", "south-b", "west-b"):
            self.assertEqual(
                statuses[cluster], "not_started_budget", cluster
            )
        east_a = next(
            item for item in result["clusters"] if item["cluster"] == "east-a"
        )
        self.assertEqual(
            east_a["dispatch_reason_code"], "ROUND_BUDGET_EXHAUSTED"
        )
        self.assertEqual(east_a["ledger"]["replicas"]["used"], 0)

        abandoned_db = self.tempdir / "abandoned.sqlite3"
        abandoned = Engine(
            samples_dir=SAMPLES, db_path=abandoned_db
        )
        abandoned.load()
        result = abandoned.apply(abandoned=["south-a"])
        south = next(
            item for item in result["clusters"] if item["cluster"] == "south-a"
        )
        self.assertEqual(south["dispatch_status"], "abandoned")
        self.assertEqual(south["dispatch_reason_code"], "OPERATOR_ABANDONED")
        self.assertEqual(south["ledger"]["replicas"]["used"], 0)

    def test_crash_after_start_and_after_ledger_rollback_recover(self) -> None:
        for crash_point in ("after_dispatch_start", "after_ledger_update"):
            with self.subTest(crash_point=crash_point):
                db = self.tempdir / f"crash-{crash_point}.sqlite3"
                load = self.run_cli("load", db=db)
                self.assertEqual(load.returncode, 0, load.stderr)
                crashed = self.run_cli(
                    "apply",
                    db=db,
                    env={"MCDE_CRASH_AT": crash_point},
                )
                self.assertEqual(crashed.returncode, 137)
                before = self.run_cli("status", db=db)
                self.assertEqual(before.returncode, 0)
                before_status = json.loads(before.stdout)
                east_a = next(
                    item
                    for item in before_status["clusters"]
                    if item["cluster"] == "east-a"
                )
                self.assertEqual(east_a["last_dispatch"]["status"], "started")
                self.assertEqual(east_a["replicas"]["used"], 0)

                resumed = self.run_cli("resume", db=db)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                resumed_result = json.loads(resumed.stdout)
                statuses = {
                    item["cluster"]: item["dispatch_status"]
                    for item in resumed_result["clusters"]
                }
                self.assertEqual(statuses["east-a"], "applied")
                self.assertEqual(statuses["south-a"], "applied")
                self.assertEqual(statuses["south-b"], "applied")
                self.assertEqual(statuses["west-b"], "failed_after_start")
                conn = sqlite3.connect(db)
                try:
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM cluster_commits "
                            "WHERE cluster_id='east-a' AND version=1"
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT replicas_used FROM ledger "
                            "WHERE cluster_id='east-a'"
                        ).fetchone()[0],
                        9,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM approval_uses "
                            "WHERE approval_id='APR-EAST-A-REPLICAS'"
                        ).fetchone()[0],
                        1,
                    )
                finally:
                    conn.close()
                verified = self.run_cli("verify", db=db)
                self.assertEqual(verified.returncode, 0, verified.stderr)
                self.assertTrue(json.loads(verified.stdout)["ok"])

    def test_concurrent_apply_has_single_version_and_no_overdraw(self) -> None:
        load = self.run_cli("load")
        self.assertEqual(load.returncode, 0)
        processes = []
        for _ in range(3):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "mcde",
                        "--db",
                        str(self.db),
                        "--samples",
                        str(self.samples),
                        "apply",
                    ],
                    cwd=REPO_ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for process in processes:
            _, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(
            self.query_one("SELECT COUNT(*) FROM config_versions"), 1
        )
        self.assertEqual(
            self.query_one("SELECT COUNT(*) FROM cluster_commits"), 3
        )
        self.assertEqual(
            self.query_one(
                "SELECT COUNT(*) FROM approval_uses "
                "WHERE approval_id='APR-EAST-A-REPLICAS'"
            ),
            1,
        )
        self.assertEqual(
            self.query_one(
                "SELECT replicas_used FROM ledger WHERE cluster_id='east-a'"
            ),
            9,
        )
        self.assertEqual(
            self.query_one(
                "SELECT COUNT(*) FROM ledger "
                "WHERE replicas_used > replicas_capacity "
                "OR resources_used > resources_capacity"
            ),
            0,
        )

    def test_version_rollback_is_rejected(self) -> None:
        engine = self.engine()
        engine.load()
        first = engine.apply()
        self.assertEqual(first["version"], 1)

        global_path = self.samples / "global.yaml"
        global_doc = yaml.safe_load(global_path.read_text(encoding="utf-8"))
        global_doc["apps"][0]["timeout_ms"] = 501
        global_path.write_text(
            yaml.safe_dump(global_doc, sort_keys=False), encoding="utf-8"
        )
        engine.load()
        second = engine.apply()
        self.assertEqual(second["version"], 2)

        global_doc["apps"][0]["timeout_ms"] = 502
        global_path.write_text(
            yaml.safe_dump(global_doc, sort_keys=False), encoding="utf-8"
        )
        engine.load()
        with self.assertRaises(EngineError) as raised:
            engine.apply(requested_version=1)
        self.assertEqual(raised.exception.code, E_VERSION_ROLLBACK)
        self.assertEqual(engine.status()["active_version"], 2)


if __name__ == "__main__":
    unittest.main()

