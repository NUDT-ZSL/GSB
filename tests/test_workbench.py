from __future__ import annotations

import itertools
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

from workbench.canonical import canonical_dumps  # noqa: E402
from workbench.errors import ResultMismatchError, WorkbenchError  # noqa: E402
from workbench.ioformats import load_registry  # noqa: E402
from workbench.pipeline import (  # noqa: E402
    cmd_init_registry,
    cmd_load_observations,
    cmd_load_shard_manifest,
    cmd_merge,
    cmd_patch,
    cmd_reconcile,
    cmd_resume,
    cmd_status,
)
from workbench.seed import BASE_CLOCK_MS, generate_sample  # noqa: E402
from workbench.rules import load_v2_bundle  # noqa: E402
from workbench.verification import verify_sources  # noqa: E402


CLOCK = BASE_CLOCK_MS


class WorkbenchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="gsb-test-")
        cls.root = Path(cls.temp.name)
        cls.sample = cls.root / "sample"
        generate_sample(cls.sample)
        cls.baseline_db = cls.root / "baseline.db"
        cls.baseline = cls._build_baseline(cls.baseline_db)
        cls.baseline_hash = cls.baseline["ledger_hash"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    @classmethod
    def _build_baseline(cls, db: Path):
        cmd_init_registry(db, cls.sample / "registry.json", CLOCK)
        cmd_merge(
            db,
            v1_path=cls.sample / "rules_v1.json",
            v2_manifest_path=cls.sample / "rules_v2_manifest.json",
            clock_ms=CLOCK,
        )
        cmd_load_observations(db, cls.sample / "observations.jsonl")
        cmd_reconcile(db, CLOCK)
        return cmd_status(db)

    def _new_db(self, name: str) -> Path:
        return self.root / f"{name}.db"

    def _init_rules(self, db: Path) -> None:
        cmd_init_registry(db, self.sample / "registry.json", CLOCK)
        cmd_merge(
            db,
            v1_path=self.sample / "rules_v1.json",
            v2_manifest_path=self.sample / "rules_v2_manifest.json",
            clock_ms=CLOCK,
        )

    def _instance(self, ledger: dict, instance_id: str) -> dict:
        return next(item for item in ledger["instances"] if item["instance_id"] == instance_id)

    def test_01_sample_and_baseline_health_machine(self) -> None:
        alpha = self._instance(self.baseline, "inst-alpha")
        beta = self._instance(self.baseline, "inst-beta")
        delta = self._instance(self.baseline, "inst-delta")
        self.assertEqual("healthy", alpha["health_status"])
        self.assertEqual(0, alpha["consecutive_failures"])
        states = [(d["seq"], d["verdict"], d["health_after"], d["consecutive_failures_after"]) for d in alpha["decisions"]]
        self.assertIn((3, "status_mismatch", "suspect", 1), states)
        self.assertIn((5, "status_mismatch", "lost", 3), states)
        self.assertEqual("suspect", self._instance(self.baseline, "inst-beta")["decisions"][2]["health_after"])
        self.assertTrue(all(d["verdict"] == "quarantined" for d in delta["decisions"]))
        self.assertEqual(0, delta["consecutive_failures"])
        self.assertEqual("rule_tombstoned", delta["rule_invalid_reason"])
        self.assertEqual(2, beta["rule_generation"])
        self.assertEqual(4, self._instance(self.baseline, "inst-gamma")["rule_generation"])

    def test_02_invalid_registry_fields_have_codes(self) -> None:
        bad_dir = self.root / "invalid"
        bad_dir.mkdir(exist_ok=True)
        raw = json.loads((self.sample / "registry.json").read_text())
        duplicate = json.loads(json.dumps(raw))
        duplicate["instances"][1]["instance_id"] = duplicate["instances"][0]["instance_id"]
        path = bad_dir / "duplicate.json"
        path.write_text(json.dumps(duplicate))
        with self.assertRaises(WorkbenchError) as ctx:
            load_registry(path)
        self.assertEqual("E_DUPLICATE_INSTANCE", ctx.exception.code)
        self.assertIn("instance_id", ctx.exception.details)

        bad_endpoint = json.loads(json.dumps(raw))
        bad_endpoint["instances"][0]["endpoint"] = "http://10.0.0.1:8080/health"
        path = bad_dir / "endpoint.json"
        path.write_text(json.dumps(bad_endpoint))
        with self.assertRaises(WorkbenchError) as ctx:
            load_registry(path)
        self.assertEqual("E_ENDPOINT_NOT_LOOPBACK", ctx.exception.code)

        bad_codes = json.loads(json.dumps(raw))
        bad_codes["instances"][0]["expected_status_codes"] = [20]
        path = bad_dir / "codes.json"
        path.write_text(json.dumps(bad_codes))
        with self.assertRaises(WorkbenchError) as ctx:
            load_registry(path)
        self.assertEqual("E_STATUS_CODES_INVALID", ctx.exception.code)

    def test_03_observation_digest_and_sequence_errors(self) -> None:
        lines = (self.sample / "observations.jsonl").read_text().splitlines()
        record = json.loads(lines[0])
        record["status_code"] = 599
        bad = self.root / "bad_digest.jsonl"
        bad.write_text(json.dumps(record) + "\n")
        with self.assertRaises(WorkbenchError) as ctx:
            cmd_load_observations(self._new_db("digest-unused"), bad)
        self.assertEqual("E_OBSERVATION_DIGEST_MISMATCH", ctx.exception.code)
        self.assertIn("expected_digest", ctx.exception.details)

        from workbench.ioformats import parse_jsonl_text

        r1 = json.loads(lines[0])
        r2 = json.loads(lines[1])
        # Lines are sorted by seq then instance; find two same-instance records and reverse them.
        by_instance = {}
        for line in lines:
            item = json.loads(line)
            by_instance.setdefault(item["instance_id"], []).append(item)
        pair = by_instance["inst-alpha"][:2]
        with self.assertRaises(WorkbenchError) as ctx:
            parse_jsonl_text("\n".join(json.dumps(x) for x in reversed(pair)), "<reversed>")
        self.assertEqual("E_OBSERVATION_SEQUENCE_ORDER", ctx.exception.code)

    def test_04_v2_shard_hash_and_generation_monotonicity(self) -> None:
        bad_v2 = self.root / "bad-v2"
        shutil.copytree(self.sample, bad_v2)
        shard = bad_v2 / "rules_v2_shard_00.json"
        shard.write_text(shard.read_text() + "\n")
        with self.assertRaises(WorkbenchError) as ctx:
            load_v2_bundle(bad_v2 / "rules_v2_manifest.json")
        self.assertEqual("E_SHARD_DIGEST_MISMATCH", ctx.exception.code)

        import sqlite3

        monotonic_dir = self.root / "bad-gen"
        shutil.copytree(self.sample, monotonic_dir)
        conn = sqlite3.connect(monotonic_dir / "rules_v2.db")
        conn.execute("INSERT INTO rule_generation_history(rule_key,generation,changed_at_ms,ordinal) VALUES(?,?,?,?)", ("rule-alpha", 0, 1, 99))
        conn.commit()
        conn.close()
        with self.assertRaises(WorkbenchError) as ctx:
            load_v2_bundle(monotonic_dir / "rules_v2_manifest.json")
        self.assertEqual("E_RULE_GENERATION_NONMONOTONIC", ctx.exception.code)

    def test_05_all_shard_orders_converge_bitwise(self) -> None:
        shard_ids = ["obs-shard-001", "obs-shard-002", "obs-shard-003"]
        hashes = set()
        for order in itertools.permutations(shard_ids):
            db = self._new_db("order-" + "-".join(order))
            self._init_rules(db)
            for shard_id in order:
                cmd_load_shard_manifest(db, self.sample / "observation_shards_manifest.json", only_shard=shard_id)
                cmd_reconcile(db, CLOCK)
            ledger = cmd_status(db)
            self.assertEqual(self.baseline_hash, ledger["ledger_hash"], order)
            hashes.add(canonical_dumps(ledger))
        self.assertEqual(1, len(hashes))

    def test_06_missing_middle_shard_blocks_later_conclusions(self) -> None:
        db = self._new_db("missing-middle")
        self._init_rules(db)
        manifest = self.sample / "observation_shards_manifest.json"
        cmd_load_shard_manifest(db, manifest, only_shard="obs-shard-001")
        cmd_reconcile(db, CLOCK)
        after_first = cmd_status(db)
        self.assertEqual(2, self._instance(after_first, "inst-alpha")["committed_through_seq"])

        cmd_load_shard_manifest(db, manifest, only_shard="obs-shard-003")
        cmd_reconcile(db, CLOCK)
        blocked = cmd_status(db)
        alpha = self._instance(blocked, "inst-alpha")
        self.assertEqual(2, alpha["committed_through_seq"])
        self.assertFalse(alpha["window_complete"])
        self.assertEqual("obs-shard-002", alpha["blocked_intervals"][0]["shard_id"])
        self.assertTrue(all(d["seq"] <= 2 for d in alpha["decisions"]))

        cmd_load_shard_manifest(db, manifest, only_shard="obs-shard-002")
        cmd_reconcile(db, CLOCK)
        self.assertEqual(self.baseline_hash, cmd_status(db)["ledger_hash"])

    def test_07_verify_reports_first_mismatch(self) -> None:
        tampered_path = self.root / "tampered_ledger.json"
        ledger = json.loads(canonical_dumps(self.baseline))
        alpha = next(i for i in ledger["instances"] if i["instance_id"] == "inst-alpha")
        decision = next(d for d in alpha["decisions"] if d["seq"] == 3)
        decision["verdict"] = "success"
        decision["health_after"] = "healthy"
        tampered_path.write_text(json.dumps(ledger, sort_keys=True))
        expected_file = self.root / "baseline_ledger.json"
        expected_file.write_text(json.dumps(self.baseline, sort_keys=True))
        with self.assertRaises(ResultMismatchError) as ctx:
            verify_sources(expected_path=expected_file, actual_paths=[tampered_path], actual_dbs=[])
        self.assertEqual("E_RESULT_MISMATCH", ctx.exception.code)
        self.assertEqual("inst-alpha", ctx.exception.details["first_instance_id"])
        self.assertEqual(3, ctx.exception.details["first_observation_seq"])

    def test_08_patch_closure_and_idempotency(self) -> None:
        db = self._new_db("patch-alpha")
        self._build_baseline(db)
        first = cmd_patch(db, self.sample / "patches" / "patch_alpha_tombstone.json", CLOCK + 120_000)
        report = first["patch_report"]
        self.assertEqual(["rule-alpha", "rule-beta", "rule-gamma"], report["closure"]["closure_rule_keys"])
        result = first["reconcile"]
        self.assertEqual({"inst-alpha", "inst-beta", "inst-gamma"}, {k for k, v in result["actions"].items() if v == "recompute"})
        self.assertEqual({"inst-delta", "inst-epsilon"}, {k for k, v in result["actions"].items() if v == "reuse"})
        second = cmd_patch(db, self.sample / "patches" / "patch_alpha_tombstone.json", CLOCK + 120_000)
        self.assertTrue(second["idempotent"])
        status = cmd_status(db)
        self.assertFalse(self._instance(status, "inst-beta")["rule_enabled"])

    def test_09_effective_time_window_and_dependency_patch(self) -> None:
        db = self._new_db("patch-window")
        self._build_baseline(db)
        cmd_patch(db, self.sample / "patches" / "patch_beta_effective.json", CLOCK + 120_000)
        beta = self._instance(cmd_status(db), "inst-beta")
        self.assertFalse(beta["observation_window"]["consistent"])
        self.assertEqual(2, beta["observation_window"]["observations_before_effective"])

        db2 = self._new_db("patch-dep")
        self._build_baseline(db2)
        cmd_patch(db2, self.sample / "patches" / "patch_epsilon_dependency.json", CLOCK + 120_000)
        epsilon = self._instance(cmd_status(db2), "inst-epsilon")
        self.assertFalse(epsilon["rule_enabled"])
        self.assertEqual("upstream_rule_tombstoned", epsilon["rule_invalid_reason"])

    def _run_cli(self, db: Path, *args: str, crash_point: str | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        if crash_point:
            env["WB_CRASH_POINT"] = crash_point
        return subprocess.run(
            [sys.executable, "-m", "workbench", *args, "--db", str(db)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def test_10_crash_recovery_matches_baseline(self) -> None:
        db = self._new_db("crash-buffer")
        self._run_cli(db, "load", "--registry", str(self.sample / "registry.json"), "--clock-ms", str(CLOCK)).check_returncode()
        self._run_cli(db, "merge", "--v1", str(self.sample / "rules_v1.json"), "--v2-manifest", str(self.sample / "rules_v2_manifest.json"), "--clock-ms", str(CLOCK)).check_returncode()
        crashed = self._run_cli(db, "load", "--observations", str(self.sample / "observations.jsonl"), crash_point="after_pending_commit")
        self.assertNotEqual(0, crashed.returncode)
        recovered = cmd_resume(db, CLOCK)
        self.assertGreaterEqual(len(recovered["recovery"]["buffered_batches_before"]), 1)
        self.assertEqual(self.baseline_hash, cmd_status(db)["ledger_hash"])

        db2 = self._new_db("crash-stage")
        self._build_baseline(db2)
        # baseline already committed; add no new observations, so force a rule patch crash instead.
        crashed = self._run_cli(
            db2,
            "patch",
            "--patch",
            str(self.sample / "patches" / "patch_epsilon_dependency.json"),
            "--clock-ms",
            str(CLOCK + 120_000),
            crash_point="after_stage_before_pointer",
        )
        self.assertNotEqual(0, crashed.returncode)
        cmd_resume(db2, CLOCK)
        # The patch transaction rolled back; resume alone does not submit patch, so ledger remains baseline.
        self.assertEqual(self.baseline_hash, cmd_status(db2)["ledger_hash"])
        applied = cmd_patch(db2, self.sample / "patches" / "patch_epsilon_dependency.json", CLOCK + 120_000)
        self.assertFalse(applied.get("idempotent", False))

    def test_11_concurrent_processes_are_idempotent(self) -> None:
        db = self._new_db("concurrent")
        self._init_rules(db)
        manifest = str(self.sample / "observation_shards_manifest.json")
        processes = []
        for shard_id in ["obs-shard-001", "obs-shard-002", "obs-shard-003", "obs-shard-001"]:
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "workbench", "load", "--db", str(db), "--shard-manifest", manifest, "--only-shard", shard_id],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, "PYTHONPATH": str(ROOT)},
                )
            )
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(0, process.returncode, stderr.decode() + stdout.decode())
        cmd_reconcile(db, CLOCK)
        self.assertEqual(self.baseline_hash, cmd_status(db)["ledger_hash"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
